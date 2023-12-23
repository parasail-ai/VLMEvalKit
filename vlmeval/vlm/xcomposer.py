import torch
import os.path as osp
from transformers import AutoModel, AutoTokenizer
from ..smp import *

from transformers import StoppingCriteria, StoppingCriteriaList
from .utils import CustomPrompt
from PIL import Image

class StoppingCriteriaSub(StoppingCriteria):
    def __init__(self, stops=[], encounters=1):
        super().__init__()
        self.stops = stops

    def __call__(self, input_ids: torch.LongTensor, scores: torch.FloatTensor):
        for stop in self.stops:
            if torch.all((stop == input_ids[0][-len(stop):])).item():
                return True

        return False

from ..utils import DATASET_TYPE

class XComposer(CustomPrompt):

    INSTALL_REQ = False
    
    def __init__(self, model_path='internlm/internlm-xcomposer-vl-7b'):
        assert model_path is not None
        self.model_path = model_path
            
        model = AutoModel.from_pretrained(self.model_path, trust_remote_code=True).cuda().eval()
        tokenizer = AutoTokenizer.from_pretrained(self.model_path, trust_remote_code=True)
        model.tokenizer = tokenizer
        self.model = model
        self.device = self.model.internlm_model.model.embed_tokens.weight.device
        stop_words_ids = [
            torch.tensor([103027]).to(self.device), ### end of human
            torch.tensor([103028]).to(self.device), ### end of bot
        ]
        self.stopping_criteria = StoppingCriteriaList([StoppingCriteriaSub(stops=stop_words_ids)])

    def generate_vanilla(self, image_path, prompt):
        return self.model.generate(prompt, image_path)
    
    def generate_multichoice(self, image_path, prompt):
        image = Image.open(image_path).convert("RGB")
        image = self.model.vis_processor(image).unsqueeze(0).to(self.device)
        img_embeds = self.model.encode_img(image)
        prompt_segs = prompt.split('<ImageHere>')
        prompt_seg_tokens = [
            self.model.tokenizer(seg, return_tensors='pt', add_special_tokens=i == 0).to(self.device).input_ids
            for i, seg in enumerate(prompt_segs)
        ]
        prompt_seg_embs = [
            self.model.internlm_model.model.embed_tokens(seg)
            for seg in prompt_seg_tokens
        ]
        prompt_seg_embs = [prompt_seg_embs[0], img_embeds, prompt_seg_embs[1]]
        prompt_embs = torch.cat(prompt_seg_embs, dim=1)
        
        outputs = self.model.internlm_model.generate(
            inputs_embeds=prompt_embs,
            max_new_tokens=5,
            num_beams=5,
            do_sample=False,
            min_length=1,
            top_p=0.9,
            repetition_penalty=1.5,
            length_penalty=1.0,
            temperature=1.0,
            stopping_criteria=self.stopping_criteria,
        )
        output_token = outputs[0]
        if output_token[0] == 0:
            output_token = output_token[1:]
        if output_token[0] == 1:
            output_token = output_token[1:]
        output_text = self.model.tokenizer.decode(output_token, add_special_tokens=False)

        output_text = output_text.split(self.model.eoa)[0]
        output_text = output_text.split('<|Bot|>')[-1].strip()
        return output_text
    
    def generate(self, image_path, prompt, dataset=None):
        if dataset is None:
            return self.generate_vanilla(image_path, prompt)
        assert isinstance(dataset, str)
        if dataset is not None and DATASET_TYPE(dataset) == 'multi-choice':
            return self.generate_multichoice(image_path, prompt)
        else:
            return self.generate_vanilla(image_path, prompt)
    
    def multi_generate(self, image_paths, prompt, dataset=None):
        img_embeds, img_prompt = [], ''
        for i, pth in enumerate(image_paths):
            img_prompt += f'Image {i + 1}: <ImageHere>'
            image = Image.open(pth).convert('RGB')
            image = self.model.vis_processor(image).unsqueeze(0).to(self.device)
            img_embeds.append(self.model.encode_img(image))
        
        prompt = f'<|User|>: ' + img_prompt + self.model.eoh + ' <|Bot|>: '
        prompt_segs = prompt.split('<ImageHere>')
        assert len(prompt_segs) == len(img_embeds) + 1
        
        prompt_seg_tokens = [
            self.model.tokenizer(seg, return_tensors='pt', add_special_tokens=i == 0).to(self.device).input_ids
            for i, seg in enumerate(prompt_segs)
        ]
        prompt_seg_embs = [
            self.model.internlm_model.model.embed_tokens(seg)
            for seg in prompt_seg_tokens
        ]
        all_embeddings = []
        for i in range(len(img_embeds)):
            all_embeddings.extend([prompt_seg_embs[i], img_embeds[i]])
        all_embeddings.append(prompt_seg_embs[-1])
        prompt_embs = torch.cat(all_embeddings, dim=1)
        
        outputs = self.model.internlm_model.generate(
            inputs_embeds=prompt_embs,
            max_new_tokens=500,
            num_beams=5,
            do_sample=False,
            min_length=1,
            repetition_penalty=1.5,
            length_penalty=1.0,
            stopping_criteria=self.stopping_criteria,
        )
        output_token = outputs[0]
        if output_token[0] == 0:
            output_token = output_token[1:]
        if output_token[0] == 1:
            output_token = output_token[1:]
        output_text = self.model.tokenizer.decode(output_token, add_special_tokens=False)

        output_text = output_text.split(self.model.eoa)[0]
        output_text = output_text.split('<|Bot|>')[-1].strip()
        return output_text
    
    def use_custom_prompt(self, dataset):
        assert dataset is not None
        if DATASET_TYPE(dataset) == 'multi-choice':
            return True
        return False
    
    def build_prompt(self, line, dataset=None):
        assert dataset is None or isinstance(dataset, str)
        assert self.use_custom_prompt(dataset)
        tgt_path = self.dump_image(line, dataset)

        question = line['question']
        options = {
            cand: line[cand]
            for cand in string.ascii_uppercase
            if cand in line and not pd.isna(line[cand])
        }
        options_prompt = ''
        for key, item in options.items():
            options_prompt += f'{key}. {item}\n'
        hint = line['hint'] if ('hint' in line and not pd.isna(line['hint'])) else None

        img_prompt = ' <|User|>:<ImageHere>'
        txt_prompt = 'Please answer this question by choosing the correct choice.'
        context = 'N/A' if hint is None else hint
        mid_prompt = 'Context: ' + context + '\nQuestion: ' + question + '\nOptions: ' + options_prompt
        ans_prompt = ' <|Bot|>: Answer: The answer is'
        prompt = img_prompt + txt_prompt + mid_prompt + '<TOKENS_UNUSED_0>' + ans_prompt
        
        return {'image': tgt_path, 'text': prompt}
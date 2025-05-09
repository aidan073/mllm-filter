import os
import torch
from tqdm import tqdm
from PIL import Image, UnidentifiedImageError
from dotenv import load_dotenv
from huggingface_hub import login
from transformers import MllamaForConditionalGeneration, AutoProcessor, AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

def get_model(token_or_env, vision=True):
    hf_token = token_or_env
    if(hf_token[-4:] == ".env"):
        load_dotenv(hf_token)
        hf_token = os.getenv("HF_TOKEN") 
    login(hf_token)

    if vision: # vision
        MODEL_ID = "meta-llama/Llama-3.2-11B-Vision-Instruct"
        processor = AutoProcessor.from_pretrained(MODEL_ID)
        model = MllamaForConditionalGeneration.from_pretrained(
            MODEL_ID,
            torch_dtype=torch.bfloat16,
            device_map="auto",
        )
    else: # text-only
        MODEL_ID = "meta-llama/Llama-3.1-8B-Instruct"
        processor = AutoTokenizer.from_pretrained(MODEL_ID)
        model = AutoModelForCausalLM.from_pretrained(
            MODEL_ID,
            torch_dtype=torch.bfloat16,
            device_map="auto",
        )

    # quantization has a large negative effect on perceived classification accuracy
    # BNB_CONFIG = BitsAndBytesConfig(load_in_8bit=True)
    # model = AutoModelForCausalLM.from_pretrained(
    #     MODEL_ID,
    #     quantization_config=BNB_CONFIG,
    #     device_map="auto",
    #     trust_remote_code=True,
    # )

    return model, processor

def vision_filter(model, processor, metadata, caption_column, image_column, prompt, output_path, has_header, delim="\t", threshold:int=0.5, save_every:int=None, max_steps:int=10, topk:int=None, keep_corrupted:bool=False):
    """
    Filter a dataset using an MLLM
    """
    msg = [
        {"role": "user", "content": [
            {"type": "image"},
            {"type": "text", "text": None}
        ]}
    ]
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    req_logit_diff = torch.log(torch.tensor(threshold / (1 - threshold))) # required logit difference to meet confidence threshold (inverse sigmoid)
    true_token_id = processor.tokenizer.convert_tokens_to_ids(processor.tokenizer.tokenize("1")[0])
    false_token_id = processor.tokenizer.convert_tokens_to_ids(processor.tokenizer.tokenize("0")[0])
    results = []
    since_last_save = 0
    missing_or_corrupted = 0
    for row in tqdm(metadata.itertuples(index=False), total=len(metadata), desc="Classifying Samples"):
        # safety saving
        if save_every and since_last_save >= save_every:
            filtered_metadata = metadata.iloc[:len(results)][results]
            filtered_metadata.to_csv(output_path, sep=delim, index=False, header=has_header, encoding='utf-8')
            print(f"Saved temporary filtered dataset to {output_path}")
            since_last_save = 0
        since_last_save += 1

        # filtering
        formatted_prompt = prompt.format(caption=getattr(row, caption_column)) if caption_column else prompt
        msg[0]["content"][1]["text"] = formatted_prompt
        input_text = processor.apply_chat_template(msg, add_generation_prompt=True)
        try:
            with Image.open(getattr(row, image_column)) as input_image:
                input = processor(input_image, input_text, add_special_tokens=False, truncation=True, return_tensors="pt").to(device)
        except FileNotFoundError:
            print(f"Image {getattr(row, image_column)} is missing.")
            missing_or_corrupted += 1
            results.append(keep_corrupted)
            continue
        except UnidentifiedImageError:
            print(f"Image {getattr(row, image_column)} is corrupted.")
            missing_or_corrupted += 1
            results.append(keep_corrupted)
            continue

        results.append(vision_classify(model, input, req_logit_diff, true_token_id, false_token_id, max_steps=max_steps, topk=topk))

    return results, missing_or_corrupted

def text_filter(model, tokenizer, metadata, caption_column, prompt, output_path, has_header, delim="\t", threshold:int=0.5, save_every:int=None, max_steps:int=10, topk:int=None):
    """
    Filter a dataset using an LLM
    """
    msg = [
        {"role": "system", "content": "You are an AI assistant that follows the user's directions."},
        {"role": "user", "content": None}
    ]
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    req_logit_diff = torch.log(torch.tensor(threshold / (1 - threshold))) # required logit difference to meet confidence threshold (inverse sigmoid)
    true_token_id = tokenizer.convert_tokens_to_ids(tokenizer.tokenize("1")[0])
    false_token_id = tokenizer.convert_tokens_to_ids(tokenizer.tokenize("0")[0])
    results = []
    since_last_save = 0
    for row in tqdm(metadata.itertuples(index=False), total=len(metadata), desc="Classifying Samples"):
        # safety saving
        if save_every and since_last_save >= save_every:
            filtered_metadata = metadata.iloc[:len(results)][results]
            filtered_metadata.to_csv(output_path, sep=delim, index=False, header=has_header, encoding='utf-8')
            print(f"Saved temporary filtered dataset to {output_path}")
            since_last_save = 0
        since_last_save += 1
        
        formatted_prompt = prompt.format(caption=getattr(row, caption_column))
        msg[1]["content"] = formatted_prompt
        input = tokenizer.apply_chat_template(msg, add_generation_prompt=True, return_tensors="pt").to(device)
        results.append(text_classify(model, input, req_logit_diff, true_token_id, false_token_id, max_steps=max_steps, topk=topk))

    return results

def vision_classify(model, input, req_logit_diff, id_1, id_0, max_steps=10, topk=1)->bool:
    """
    Classify until the model gives a clear answer or reaches max_steps. If max_steps is reached, false classification is assumed.
    **only works for 1 sample at a time currently**
    """
    device = input["input_ids"].device
    steps = 1 if max_steps is None else max_steps

    for _ in range(steps):
        with torch.no_grad():
            output = model(**input)
            logits = output.logits[:, -1, :] # shape: (1, vocab_size)
            # print(logits[:, [id_1, id_0]])
        
        if max_steps:
            topk_ids = torch.topk(logits, topk, dim=-1).indices[0].tolist()
            # print(processor.tokenizer.decode(topk_ids[0]))
            if id_1 not in topk_ids and id_0 not in topk_ids:
                # Not ready for classification
                # Append the most likely token and update attention masks
                next_token_id = topk_ids[0]
                next_token_tensor = torch.tensor([[next_token_id]], device=device)
                input["input_ids"] = torch.cat([input["input_ids"], next_token_tensor], dim=1)
                next_attention_mask = torch.ones_like(next_token_tensor)
                input["attention_mask"] = torch.cat([input["attention_mask"], next_attention_mask], dim=1)
                next_cross_attention_mask = torch.tensor([[[[1, 1, 0, 0]]]], device=device)
                input["cross_attention_mask"] = torch.cat([input["cross_attention_mask"], next_cross_attention_mask], dim=1)
                "[[[[1,1,0,0]]],[[[1,1,0,0]]],...]"
                continue
        
        # Making a classification
        target_logits = logits[:, [id_1, id_0]]
        difference = target_logits[:, 0] - target_logits[:, 1]
        prediction = difference >= req_logit_diff

        return prediction.item()
        
    print(f"Reached classification attempt limit of {max_steps}")
    return True

def text_classify(model, input, req_logit_diff, id_1, id_0, max_steps=10, topk=1)->bool:
    """
    Classify until the model gives a clear answer or reaches max_steps. If max_steps is reached, false classification is assumed.
    **only works for 1 sample at a time currently**
    """
    device = input.device
    steps = 1 if max_steps is None else max_steps

    for _ in range(steps):
        with torch.no_grad():
            output = model(input)
            logits = output.logits[:, -1, :] # shape: (1, vocab_size)
            # print(logits[:, [id_1, id_0]])
        
        if max_steps:
            topk_ids = torch.topk(logits, topk, dim=-1).indices[0].tolist()
            # print(processor.tokenizer.decode(topk_ids[0]))
            if id_1 not in topk_ids and id_0 not in topk_ids:
                # Not ready for classification
                # Append the most likely token and update attention masks
                next_token_id = topk_ids[0]
                next_token_tensor = torch.tensor([[next_token_id]], device=device)
                input["input_ids"] = torch.cat([input["input_ids"], next_token_tensor], dim=1)
                next_attention_mask = torch.ones_like(next_token_tensor)
                input["attention_mask"] = torch.cat([input["attention_mask"], next_attention_mask], dim=1)
                continue
        
        # Making a classification
        target_logits = logits[:, [id_1, id_0]]
        difference = target_logits[:, 0] - target_logits[:, 1]
        prediction = difference >= req_logit_diff

        return prediction.item()
        
    print(f"Reached classification attempt limit of {max_steps}")
    return True
from transformers import AutoModelForCausalLM, AutoModel, AutoModelForSequenceClassification, AutoTokenizer, PreTrainedModel
from dataclasses import dataclass
from typing import Optional, Union, Tuple
import random
import torch
import torch.nn.functional as F
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
# from torch.utils.tensorboard import SummaryWriter
from typing import Callable, Dict, List, Optional, Tuple, Union, Any
from copy import deepcopy
from datasets import load_dataset
from reward_func import *
import wandb

import os
os.environ['CUDA_VISIBLE_DEVICES'] = '2'

SYSTEM_PROMPT = "Let's think step by step and output the final answer within \\boxed{}."

class GSM8KDataset(Dataset):
    def __init__(self, data_path, tokenizer, filter_dataset_by_len=True, grpo_args=None):
        
        self.tokenizer = tokenizer
        data = load_dataset(data_path)
        self.data = data['train']
        
        # Filter dataset by length if enabled
        if filter_dataset_by_len and grpo_args is not None:
            print(f"Original dataset size: {len(self.data)}")
            filtered_data = []
            
            for sample in self.data:
                # Get prompt and answer
                prompt = sample["question"]
                answer = sample["answer"]
                
                # Apply the same chat template as in training to get accurate length
                input_text = self.tokenizer.apply_chat_template(
                    [{"role": "system", 'content': SYSTEM_PROMPT}, 
                     {"role": "user", 'content': prompt}], 
                    add_generation_prompt=True, 
                    tokenize=False
                )
                
                # Tokenize to check actual length after chat template
                prompt_tokens = self.tokenizer.encode(input_text, add_special_tokens=False)
                
                # Filter based on max lengths - keep only if it won't be truncated
                if (len(prompt_tokens) <= grpo_args.max_prompt_length):
                    filtered_data.append(sample)
            
            self.data = filtered_data
            print(f"Filtered dataset size: {len(self.data)}")
            print(f"Filtered out: {len(data['train']) - len(self.data)} samples")
            if len(self.data) > 0:
                print(f"Retention rate: {len(self.data) / len(data['train']) * 100:.2f}%")
  
    def __len__(self):
        return len(self.data)
    
    
    
    def __getitem__(self, index):
        sample = self.data[index]
        # prompt = self.tokenizer.apply_chat_template(sample['prompt'], tokenize=False, add_generation_prompt=True)
        answer = sample['answer_only']
        # prompt = sample['question_zh']
        prompt = sample['question']
        return {'prompt': prompt, 'answer': answer}


@dataclass
class Samples:
    prompt_response_ids: torch.Tensor
    response_ids: torch.Tensor
    prompt: Any
    answer: Any
    attention_mask: Optional[torch.LongTensor]
    action_mask: Optional[torch.BoolTensor]
    num_actions: Union[int, torch.Tensor]
    response_length: int


class GRPOArguments:
    
    output_dir = './output'
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    lr = 1e-6
    save_steps = 100
    epoch = 3
    num_generations = 4 # 组内样本数
    max_prompt_length = 512 # 最大输入长度
    max_generate_length = 1024 # 最大输出长度
    reward_weights : List[float] = None # 奖励的权重（多个奖励函数）
    beta = 1e-2 # KL散度的系数，为0则忽略KL散度，即不使用参考模型
    clip_eps = 0.2
    gradient_accumulation_steps = 2 # 梯度累加
    num_iterations = 1 # 采样一次样本训练模型轮数
    batch_size = 1
    use_wandb = False # 是否使用wandb记录
    wandb_project = "grpo-training" # wandb项目名称
    wandb_run_name = None # wandb运行名称，默认自动生成

class GRPOTrainer:
    def __init__(self,
        model = None,
        reward_funcs: Union[List[str], List[Callable]] = None,
        args = None,
        train_dataset: Optional[Union[Dataset]] = None,
        eval_dataset: Optional[Union[Dataset]] = None,
        tokenizer = None,
        reward_tokenizers = None):

        self.args = args
        # 加载模型
        if isinstance(model, str):
            model = AutoModelForCausalLM.from_pretrained(model)
        self.model = model.to(self.args.device)
        
        # 是否使用参考模型
        self.ref_model = None
        if self.args.beta != 0.0:
            self.ref_model = deepcopy(model)
            self.ref_model.eval()
    
        
        if isinstance(tokenizer, str):
            tokenizer = AutoTokenizer.from_pretrained(tokenizer)
        
        self.tokenizer = self.get_tokenizer(tokenizer)
        
        if isinstance(reward_funcs, str):
            reward_funcs = [reward_funcs]
        
        for i, reward_func in enumerate(reward_funcs):
            # 如果奖励函数为字符串，表示使用的是奖励模型，则加载模型
            if isinstance(reward_func, str):
                reward_funcs[i] = AutoModelForSequenceClassification.from_pretrained(
                    reward_func, num_labels=1).to(self.args.device)
        
        self.reward_funcs = reward_funcs
        
        if reward_tokenizers is None:
            reward_tokenizers = [None] * len(reward_funcs)
            
        elif isinstance(reward_tokenizers, str):
            reward_tokenizers = [reward_tokenizers]
            
        else:
            if len(reward_tokenizers) != len(reward_funcs):
                raise ValueError("Length of reward_tokenizers must be equal to the number of reward_funcs.")
            
        for i, (reward_tokenizer, reward_func) in enumerate(zip(reward_tokenizers, reward_funcs)):
            if isinstance(reward_func, PreTrainedModel):
                if reward_tokenizer is None:
                    reward_tokenizer = AutoTokenizer.from_pretrained(reward_func.config._name_or_path)
                if reward_tokenizer.pad_token_id is None:
                    reward_tokenizer.pad_token = reward_tokenizer.eos_token
                
                reward_func.config.pad_token_id = reward_tokenizer.pad_token_id
                reward_tokenizers[i] = reward_tokenizer
        self.reward_tokenizers = reward_tokenizers
        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=self.args.lr)
        self.train_dataset = train_dataset
        self.eval_dataset = eval_dataset
        
        # 缓存已经生成的数据的一个批次的数据，可供模型多次训练迭代，无需重新生成
        self.input_buffer = [None] * self.args.gradient_accumulation_steps
        
        # 模型更新的次数
        self.update_steps = 0
        
        # 初始化wandb
        if self.args.use_wandb:
            wandb.init(
                project=self.args.wandb_project,
                name=self.args.wandb_run_name,
                config={
                    "learning_rate": self.args.lr,
                    "epochs": self.args.epoch,
                    "batch_size": self.args.batch_size,
                    "num_generations": self.args.num_generations,
                    "max_prompt_length": self.args.max_prompt_length,
                    "max_generate_length": self.args.max_generate_length,
                    "beta": self.args.beta,
                    "clip_eps": self.args.clip_eps,
                    "gradient_accumulation_steps": self.args.gradient_accumulation_steps,
                    "num_iterations": self.args.num_iterations,
                }
            ) 
    def get_tokenizer(self, tokenizer):
        tokenizer.padding_side = "left"
        return tokenizer
    
    # Generate samples for each prompt in the inputs
    def generate_samples(self, inputs):
        samples_list = []
        self.model.eval()
        prompts = [prompt for prompt in inputs['prompt']]
        answers = [None] * len(prompts)
        
        if 'answer' in inputs:
            answers = [answer for answer in inputs['answer']]
        
        max_length = self.args.max_generate_length + self.args.max_prompt_length
        for prompt, answer in zip(prompts, answers):
            # For each prompt, generate a group of samples
            
            # Apply chat template with system prompt
            input_text = self.tokenizer.apply_chat_template([{"role": "system", 'content': SYSTEM_PROMPT}, {"role": "user", 'content': prompt}], add_generation_prompt=True, tokenize=False)
            
            # Generate input data for a group
            inputs = self.tokenizer([input_text] * self.args.num_generations, padding='max_length', max_length=self.args.max_prompt_length, truncation=True, return_tensors='pt')
            
            prompt_ids = inputs['input_ids']
            
            with torch.no_grad():
                prompt_response_ids = self.model.generate(
                    **inputs.to(self.args.device), 
                    max_new_tokens = self.args.max_generate_length,
                    temperature=0.9,
                    top_p = 1,
                    top_k = 50)
                
            if prompt_response_ids.size(1) >= max_length:
                prompt_response_ids = prompt_response_ids[:, :max_length]
            else:
                # Pad sequences to max_length
                prompt_response_ids = torch.cat([prompt_response_ids, torch.full((prompt_response_ids.size(0), max_length - prompt_response_ids.size(1)), fill_value=self.tokenizer.pad_token_id, device=prompt_response_ids.device)], dim=1)
          
            attention_mask = (prompt_response_ids.ne(self.tokenizer.pad_token_id)).to(dtype=torch.long)
            
            # [B, max gen tokens]
            response_ids = prompt_response_ids[:, prompt_ids.size(1):]
            action_mask = (response_ids.ne(self.tokenizer.eos_token_id) & response_ids.ne(self.tokenizer.pad_token_id)).to(dtype=torch.long)
        

            # Create Samples dataclass instance
            samples = Samples(
                prompt_response_ids=prompt_response_ids,
                response_ids=response_ids,
                prompt = prompt,
                answer = answer,
                attention_mask=attention_mask,
                action_mask=action_mask,
                num_actions=action_mask.size(1),
                response_length=action_mask.float().sum(dim=-1)
            )
            samples_list.append(samples)

        return samples_list
    
    # 生成经验(优势、token的概率分布)
    def generate_experiences(self, inputs):
        
        self.model.eval()
        samples_list = self.generate_samples(inputs)
        
        batch_prompt_response_ids = []
        batch_attention_mask = []
        batch_action_mask = []
        batch_advantages = []
        batch_old_action_log_probs = []
        batch_ref_action_log_probs = []
        
        for samples in samples_list:
            prompt_response_ids = samples.prompt_response_ids # shape: (num_generations, seq_len)
            response_ids = samples.response_ids # shape: (num_generations, seq_len)
            answer = samples.answer
            attention_mask = samples.attention_mask # shape: (num_generations, seq_len)
            action_mask = samples.action_mask # shape: (num_generations, seq_len)
            num_actions = samples.num_actions
            prompt = samples.prompt
            batch_prompt_response_ids.append(prompt_response_ids)
            batch_attention_mask.append(attention_mask)
            batch_action_mask.append(action_mask)
            
            with torch.no_grad():
                # 计算策略模型输出token的概率
                old_action_log_probs = self.get_action_log_probs(self.model, prompt_response_ids, attention_mask, num_actions)
                batch_old_action_log_probs.append(old_action_log_probs)
                
                # 是否使用参考模型
                if self.ref_model:
                    #计算参考模型输出token的概率
                    ref_action_log_probs = self.get_action_log_probs(self.ref_model, prompt_response_ids, attention_mask, num_actions)
                    batch_ref_action_log_probs.append(ref_action_log_probs)
                
                # 存储各个奖励函数在一个group内各个响应的奖励
                rewards_per_func = torch.zeros(len(self.reward_funcs), self.args.num_generations, device=self.args.device)
                
                # 将输出转换成文本
                response_texts = self.tokenizer.batch_decode(response_ids, skip_special_tokens=True)
                prompt_texts = [prompt] * len(response_texts)
                prompt_response_texts = [prompt + response for prompt, response in zip(prompt_texts, response_texts)]
                
                for i, (reward_func, reward_tokenizer) in enumerate(
                    zip(self.reward_funcs, self.reward_tokenizers)
                ):
                    if isinstance(reward_func, PreTrainedModel):
                        with torch.inference_mode():
                            reward_model_inputs = reward_tokenizer(prompt_response_texts, return_tensors="pt", padding=True)
                            rewards_per_func[i] = reward_func(**reward_model_inputs.to(self.args.device)).logits.squeeze(-1)
                    
                    else:
                        answers = [answer] * len(prompt_texts)
                        output_reward_func = reward_func(prompts=prompt_texts, responses=response_texts, answers=answers)
                        output_reward_func = [reward if reward is not None else torch.nan for reward in output_reward_func]
                        rewards_per_func[i] = torch.tensor(output_reward_func, dtype=torch.float32, device=self.args.device)
                
                # rewards_per_func: [num_funcs, num_generations]
                if not self.args.reward_weights:
                    self.args.reward_weights = [1.0] * len(self.reward_funcs)
                if len(self.args.reward_weights) != len(self.reward_funcs):
                    raise ValueError("The number of reward weights must be equal to the number of reward functions.")
                # 乘以各个奖励函数的权重
                rewards = rewards_per_func * torch.tensor(self.args.reward_weights, dtype=torch.float32, device=rewards_per_func.device).unsqueeze(1)
                
                # rewards: [num_funcs, num_generations]
                rewards = rewards.sum(dim=0) # shape: [num_generations]
                print(f'rewards: {rewards}')
                mean_group_rewards = rewards.mean()
                std_group_rewards = rewards.std()
                
                # 记录奖励到wandb
                if self.args.use_wandb:
                    reward_func_names = ['correctness', 'digit', 'hard_format', 'mark']
                    for i in range(len(self.reward_funcs)):
                        func_name = reward_func_names[i] if i < len(reward_func_names) else f'reward_{i}'
                        wandb.log({
                            f'rewards/{func_name}_mean': rewards_per_func[i].mean().item(),
                            f'rewards/{func_name}_max': rewards_per_func[i].max().item(),
                            f'rewards/{func_name}_min': rewards_per_func[i].min().item(),
                        }, step=self.update_steps)
                    
                    wandb.log({
                        'rewards_total/total_mean': mean_group_rewards.item(),
                        'rewards_total/total_std': std_group_rewards.item(),
                        'rewards_total/total_max': rewards.max().item(),
                        'rewards_total/total_min': rewards.min().item(),
                    }, step=self.update_steps)
                
                # GRPO的优势是句子粒度的，而非token粒度的
                advantages = (rewards - mean_group_rewards) / (std_group_rewards + 1e-8) # shape: [num_generations]
                batch_advantages.append(advantages)
        
               
        return {
            "prompt_response_ids": torch.cat(batch_prompt_response_ids, dim=0),
            "attention_mask": torch.cat(batch_attention_mask, dim=0),
            "action_mask": torch.cat(batch_action_mask, dim=0),
            "old_action_log_probs": torch.cat(batch_old_action_log_probs, dim=0),
            "ref_action_log_probs": torch.cat(batch_ref_action_log_probs, dim=0) if self.ref_model else None,
            "advantages": torch.cat(batch_advantages, dim=0),
        }
    
    def compute_loss(self, model, inputs):
        
        # [B, max prompt len + max gen len]
        prompt_response_ids = inputs['prompt_response_ids']
        attention_mask = inputs['attention_mask']
        
        # [B, max gen len]
        action_mask = inputs['action_mask']
        
        num_actions = action_mask.size(1)
        
        # [B, max gen len]
        action_log_probs = self.get_action_log_probs(model, prompt_response_ids, attention_mask, num_actions)
        
        
        if self.args.beta != 0.0:
            
            ref_action_log_probs = inputs['ref_action_log_probs']
            log_ratio = ref_action_log_probs - action_log_probs 
            log_ratio = log_ratio * action_mask
            
            # k3: log_ratio.exp() - 1 - log_ratio
            k3 = log_ratio.exp() - 1 - log_ratio
        
        # [B]
        advantages = inputs['advantages']
        
        old_action_log_probs = inputs['old_action_log_probs'] if self.args.num_iterations > 1 else action_log_probs.detach()
        coef_1 = torch.exp(action_log_probs - old_action_log_probs) # Importance Sampling shape: [batch_size * num_generations, num_actions]
        coef_2 = torch.clamp(coef_1, 1 - self.args.clip_eps, 1 + self.args.clip_eps)
        per_token_loss1 = coef_1 * advantages.unsqueeze(1) 
        per_token_loss2 = coef_2 * advantages.unsqueeze(1)
        
        per_token_loss = -torch.min(per_token_loss1, per_token_loss2)
        per_token_loss = per_token_loss * action_mask
        if self.args.beta != 0.0:
            per_token_loss = per_token_loss + self.args.beta * k3
        
        loss = per_token_loss.sum(dim=1) / action_mask.sum(dim=1) # shape: [batch_size * num_generations]
        loss = loss.mean()

        return loss


    def get_action_log_probs(self, model, input_ids, attention_mask, num_actions):
        
        # 计算策略模型输出token的概率
        output = model(input_ids, attention_mask=attention_mask)
        logits = output.logits
        log_probs = F.log_softmax(logits[:, :-1, :], dim=-1)
        log_probs_labels = log_probs.gather(dim=-1, index=input_ids[:, 1:].unsqueeze(-1))
        action_log_probs = log_probs_labels.squeeze(-1)[:, -num_actions:]
        return action_log_probs

    
    
    def train_step(self, model, inputs, optimizer, step):
        model.train()
        # scaler = torch.amp.GradScaler()
        # with torch.amp.autocast(device_type='cuda'):
        loss = self.compute_loss(model, inputs)
        loss = loss / self.args.gradient_accumulation_steps
        # loss = scaler.scale(loss)
        loss.backward()
        if (step + 1) % self.args.gradient_accumulation_steps == 0:
            
            optimizer.step()
            optimizer.zero_grad()
            # scaler.unscale_(optimizer)
            # scaler.step(optimizer)
            # scaler.update()
        
            # 记录到wandb
            if self.args.use_wandb:
                wandb.log({
                    'train/loss': loss.item() * self.args.gradient_accumulation_steps,
                    'train/learning_rate': self.args.lr,
                    'train/step': self.update_steps,
                }, step=self.update_steps)
            
            print(f"step: {self.update_steps}/{self.global_steps}  grpo_loss: {loss.item():.8f}")
        torch.cuda.empty_cache()

    def train(self):
        self.global_steps = self.args.num_iterations * self.args.epoch * len(self.train_dataset) // (self.args.batch_size * self.args.gradient_accumulation_steps)
        
        for _ in range(self.args.epoch):
            dataloader = DataLoader(self.train_dataset, batch_size=self.args.batch_size, shuffle=True)
            
            for idx, batch in enumerate(dataloader):
                # sample answers for each question in the batch
                inputs = self.generate_experiences(batch)
                self.input_buffer[idx % self.args.gradient_accumulation_steps] = inputs
                
                if (idx + 1) % self.args.gradient_accumulation_steps == 0:
                   
                    for _ in range(self.args.num_iterations):
                        for step, inputs in enumerate(self.input_buffer):
                            self.train_step(self.model, inputs, self.optimizer, step)
                        
                        self.update_steps += 1
                        if self.update_steps % self.args.save_steps == 0:
                            self.model.save_pretrained(self.args.output_dir + f'/checkpoint_{self.update_steps}')
                            self.tokenizer.save_pretrained(self.args.output_dir + f'/checkpoint_{self.update_steps}')
                        
                del inputs
                
    def save_model(self):
        self.model.save_pretrained(self.args.output_dir)
        self.tokenizer.save_pretrained(self.args.output_dir)
        
        # 结束wandb运行
        if self.args.use_wandb:
            wandb.finish()           

if __name__ == "__main__":

    args = GRPOArguments()

    # model_name = "Qwen/Qwen2.5-1.5B-Instruct"
    
    model_name = "Qwen/Qwen2.5-0.5B-Instruct"
    
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    model = AutoModelForCausalLM.from_pretrained(model_name)
    
    dataset_name = "meta-math/GSM8K_zh"
    
    prompts_dataset = GSM8KDataset(dataset_name, tokenizer, filter_dataset_by_len=True, grpo_args=args)
    
    reward_func_list = [boxed_correctness_reward, boxed_format_reward]
  
    trainer = GRPOTrainer(model=model,
                          reward_funcs = reward_func_list,
                          args=args,
                          train_dataset=prompts_dataset,
                          tokenizer=tokenizer)
    trainer.train()
    trainer.save_model()
    


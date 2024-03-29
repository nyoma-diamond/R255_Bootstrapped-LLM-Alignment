import os
import pickle
from argparse import ArgumentParser, RawTextHelpFormatter

from tqdm import tqdm

# High bandwidth model downloading
os.environ['HF_HUB_ENABLE_HF_TRANSFER'] = '1'

# Force CPU-only training
# os.environ["CUDA_VISIBLE_DEVICES"]=""

from transformers import AutoTokenizer
from datasets import load_dataset, Split
from trl import PPOConfig, PPOTrainer, AutoModelForCausalLMWithValueHead
from transformers import pipeline

from utils import TrainerArgs, generate_tokenize_fn, collate, format_supervisor_prompt, get_reward
from utils import TRAINER_DEFAULTS, OBJECTIVES


def initialize_option_parser():
    """
    Initializes the option parser
    :return: the option parser
    """
    parser = ArgumentParser(formatter_class=RawTextHelpFormatter)
    parser.add_argument('-b', '--batch-size',
                        action='store',
                        type=int,
                        default=TRAINER_DEFAULTS.batch_size,
                        dest='batch_size',
                        help='The number of samples per tuning batch (steps applied per-batch).')
    parser.add_argument('-m', '--mini-batch-size',
                        action='store',
                        type=int,
                        default=TRAINER_DEFAULTS.mini_batch_size,
                        dest='mini_batch_size',
                        help='The number of samples per mini-batch of each tuning batch (used by PPO trainer).')
    parser.add_argument('-d', '--out-dir',
                        action='store',
                        type=str,
                        default=TRAINER_DEFAULTS.out_dir,
                        dest='out_dir',
                        help='Path to directory to save output files to.')
    parser.add_argument('-t', '--target-model',
                        action='store',
                        type=str,
                        default='microsoft/phi-1_5',
                        dest='target_model',
                        help='Model to evaluate.'
                             '\nThis is ignored if the -n/--model-name argument is provided')
    parser.add_argument('-e', '--eval-model',
                        action='store',
                        type=str,
                        default='mistralai/Mistral-7B-Instruct-v0.2',
                        dest='eval_model',
                        help='Model to evaluate using (likely the same as the bootstrap model).')
    parser.add_argument('-c', '--cache-dir',
                        action='store',
                        type=str,
                        default=None,
                        dest='cache_dir',
                        help='Path the directory to cache pretrained models.'
                             '\nUsed by transformers library when downloading and retrieving models.')
    parser.add_argument('-n', '--model-name',
                        action='store',
                        type=str,
                        default=None,
                        dest='model_name',
                        help='Name for the newly tuned model.')

    return parser


# Modified from https://huggingface.co/docs/trl/main/en/ppo_trainer
if __name__ == '__main__':
    # Initialize option parser to manage CLI arguments
    parser = initialize_option_parser()
    args = parser.parse_args()

    trainer_args = TrainerArgs(
        batch_size=args.batch_size,
        mini_batch_size=args.mini_batch_size,
        out_dir=args.out_dir,
        cache_dir=args.cache_dir
    )

    target_model_name = args.target_model if args.model_name is None else args.model_name
    eval_model_id = args.eval_model

    model_out = f'{trainer_args.out_dir}/models'
    target_model_path = target_model_name if args.model_name is None else f'{model_out}/{target_model_name}'

    # Download and load models
    target_model = AutoModelForCausalLMWithValueHead.from_pretrained(
        target_model_path,
        device_map='auto'
    )
    target_tokenizer = AutoTokenizer.from_pretrained(
        target_model_path,
        device_map='auto'
    )

    target_tokenizer.pad_token_id = target_tokenizer.eos_token_id

    reward_model = pipeline(
        task='text-generation',
        model=eval_model_id,
        device_map='auto',
        model_kwargs=dict(cache_dir=trainer_args.cache_dir)
    )

    # Load TruthfulQA dataset. VALIDATION is the only available split
    dataset = load_dataset(path='truthful_qa', name='generation', split=Split.VALIDATION).train_test_split(train_size=0.66, shuffle=True, seed=42)
    dataset = dataset.rename_column('question', 'query')
    dataset = dataset.map(generate_tokenize_fn(target_tokenizer), batched=False)
    dataset.set_format(type='torch')

    config = PPOConfig(
        model_name=target_model,
        # accelerator_kwargs=dict(mixed_precision='fp16'),
        batch_size=trainer_args.batch_size,
        mini_batch_size=trainer_args.mini_batch_size,
        gradient_accumulation_steps=1
    )

    trainer = PPOTrainer(
        config=config,
        model=target_model,
        tokenizer=target_tokenizer,
        dataset=dataset[Split.TEST],
        data_collator=collate
    )

    generation_kwargs = dict(
        min_length=-1,
        top_k=0,
        top_p=1.0,
        do_sample=True,
        pad_token_id=target_tokenizer.eos_token_id,
        max_new_tokens=32
    )

    rewards = {objective: [] for objective, *_ in OBJECTIVES}

    for batch in tqdm(trainer.dataloader):
        query_tensors = batch['input_ids']

        #### Get response from SFTModel
        response_tensors = trainer.generate(query_tensors, return_prompt=False, **generation_kwargs)
        batch['response'] = target_tokenizer.batch_decode(response_tensors)

        for i, objective in enumerate(OBJECTIVES):
            reward_prompts = [format_supervisor_prompt(q, r, objective) for q, r in zip(batch['query'], batch['response'])]

            #### Compute reward score
            pipe_outputs = reward_model(
                reward_prompts,
                return_full_text=False,
                max_new_tokens=4,
                pad_token_id=reward_model.tokenizer.eos_token_id
            )

            rewards[objective[0]].extend([get_reward(output, as_tensor=False) for output in pipe_outputs])

    os.makedirs(f'{model_out}/{target_model_name}', exist_ok=True)
    with open(f'{model_out}/{target_model_name}/test_results.pkl', 'wb') as out_file:
        pickle.dump(rewards, out_file)

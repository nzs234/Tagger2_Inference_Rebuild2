# Add nl_client parameter to pipeline call
with open('backend/tagger2/workflow/api.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Find and replace the pipeline call
old_call = '''            report = await asyncio.to_thread(
                run_offline_pipeline,
                config,
                source_root=source_path,
                output_root=output_path,
                workspace=workspace,
                replacement_index_path=replacement_index_path,
                tag_predictor=tag_predictor,
                classification_rules=classification_rules,
                policy_config=policy_config_arg,
                token_counter=token_counter_arg,
                ocr_engine=ocr_engine,
            )'''

new_call = '''            report = await asyncio.to_thread(
                run_offline_pipeline,
                config,
                source_root=source_path,
                output_root=output_path,
                workspace=workspace,
                replacement_index_path=replacement_index_path,
                tag_predictor=tag_predictor,
                classification_rules=classification_rules,
                policy_config=policy_config_arg,
                token_counter=token_counter_arg,
                ocr_engine=ocr_engine,
                nl_client=nl_client,
            )'''

content = content.replace(old_call, new_call)

with open('backend/tagger2/workflow/api.py', 'w', encoding='utf-8') as f:
    f.write(content)
    
print('nl_client parameter added to pipeline call')

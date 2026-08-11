# Insert NL client construction code
with open('backend/tagger2/workflow/api.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Find the insertion point (after OCR engine, before policy config)
insert_marker = '''            # Policy config converted to dataclass if enabled
            policy_config_arg = None'''

nl_client_code = '''            # NL client: wrap async provider as sync client for pipeline
            nl_client = None
            if config.nl.get("enabled") and config.nl.get("api_enabled"):
                provider_id = str(config.nl.get("provider_id", ""))
                if provider_id:
                    from ...providers.manager import ProviderManager
                    from .nl_adapter import ProviderNlAdapter
                    
                    provider_manager = ProviderManager()
                    provider = provider_manager.get_provider(provider_id)
                    if provider is not None:
                        nl_client = ProviderNlAdapter(provider)

            # Policy config converted to dataclass if enabled
            policy_config_arg = None'''

content = content.replace(insert_marker, nl_client_code)

with open('backend/tagger2/workflow/api.py', 'w', encoding='utf-8') as f:
    f.write(content)
    
print('NL client construction code inserted')

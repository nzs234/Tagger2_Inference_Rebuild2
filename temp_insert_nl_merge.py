# Find the line after policy and insert NL projection merge
lines = []
with open('backend/tagger2/workflow/pipeline.py', 'r', encoding='utf-8') as f:
    for line in f:
        lines.append(line)
        # Insert after the policy_counts increment section
        if 'policy_counts[decision.appearanceNlAction] =' in line:
            # Next few lines are the closing of this block, then we insert
            pass
        elif len(lines) >= 2 and 'policy_counts[decision.appearanceNlAction]' in lines[-2]:
            # Add NL merge code after policy section
            if line.strip() == ')':
                # Find next blank line or next section
                insert_nl_merge = True
                
# Actually, let me find the exact insertion point more carefully
lines = []
with open('backend/tagger2/workflow/pipeline.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Insert NL projection merge after policy section, before token_counter
insert_marker = '''                policy_counts[decision.appearanceNlAction] = (
                    policy_counts.get(decision.appearanceNlAction, 0) + 1
                )

            if token_counter is not None:'''

nl_merge_code = '''                policy_counts[decision.appearanceNlAction] = (
                    policy_counts.get(decision.appearanceNlAction, 0) + 1
                )

            # Merge NL result if available
            if nl_projections and sample.relative_image_path in nl_projections:
                projection["nl"] = nl_projections[sample.relative_image_path]

            if token_counter is not None:'''

content = content.replace(insert_marker, nl_merge_code)

with open('backend/tagger2/workflow/pipeline.py', 'w', encoding='utf-8') as f:
    f.write(content)
    
print('NL merge code inserted')

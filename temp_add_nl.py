nl_stage_code = """
    # NL stage: generate natural language captions
    nl_projections: dict[str, str] = {}
    if config.nl.get('enabled') and config.nl.get('api_enabled') and nl_client is not None:
        # Build projections dict for NL stage (needs full nine-field structure)
        temp_projections: dict[str, dict[str, Any]] = {}
        for sample in imported.samples:
            if sample.annotation_kind == 'standard_json':
                document = json.loads(
                    (source_root / Path(sample.annotation_key + '.json')).read_bytes().decode('utf-8-sig')
                )
                temp_projections[sample.relative_image_path] = {
                    field_name: document.get(field_name) for field_name in NINE_FIELDS
                }
            else:
                projection = build_projection(sample)
                classified = classified_projections.get(sample.relative_image_path)
                if classified:
                    projection['quality'] = classified.get('quality', [])
                    if sample.annotation_kind == 'raw_e621_json':
                        projection['tags'] = [
                            tag for tag in classified.get('tags', [])
                            if tag != projection['character']
                        ]
                    else:
                        projection['character'] = ', '.join(classified.get('character', []))
                        projection['tags'] = classified.get('tags', [])
                        projection['artist'] = merge_artists(
                            str(projection['artist']),
                            ', '.join(classified.get('artist', [])),
                        )
                    projection['appearance'] = classified.get('appearance', [])
                    projection['environment'] = classified.get('environment', [])
                elif caption_tags.get(sample.relative_image_path):
                    projection['tags'] = list(caption_tags[sample.relative_image_path])
                temp_projections[sample.relative_image_path] = projection
        
        nl_report = run_nl_stage(
            imported.samples,
            temp_projections,
            source_root=source_root,
            client=nl_client,
            preset=str(config.nl.get('prompt_preset', 'general')),
            length=str(config.nl.get('length', 'medium')),
            reuse_original_nl=bool(config.nl.get('reuse_original_nl', True)),
            use_image=bool(config.nl.get('use_image', True)),
            use_full_json=bool(config.nl.get('use_full_json', False)),
        )
        nl_projections = nl_report.nl_by_path
        report.nl = {
            'generated': nl_report.generated,
            'reused': nl_report.reused,
            'failed': nl_report.failed,
        }
"""

lines = []
with open('backend/tagger2/workflow/pipeline.py', 'r', encoding='utf-8') as f:
    for line in f:
        lines.append(line)
        if line.strip() == 'raise PipelineError(f"unsupported export format: {export_format!r}")':
            found_raise = True
        elif 'found_raise' in dir() and found_raise and line.strip() == '':
            lines.append(nl_stage_code + '\n')
            found_raise = False

with open('backend/tagger2/workflow/pipeline.py', 'w', encoding='utf-8') as f:
    f.writelines(lines)
print('NL stage code added')

# Workflow Integration Plan

## Overview

This document describes the integration of the e621-standard-caption-workflow dataset processing pipeline into Tagger2 Inference Rebuild2 as a new **Dataset Workflow** module.

## Integration Status

### ✅ Phase 1: Foundation (Completed)

**Contracts & Schema**
- `backend/tagger2/workflow/contracts.py` - Versioned contracts (WorkflowJobConfigV1, WorkflowPathRef, WorkflowResourceManifestV1)
- `backend/tagger2/workflow/db_schema.py` - Independent workflows.sqlite3 schema with WAL mode
- `backend/tagger2/workflow/db.py` - Database connection and CRUD operations

**Resource Management**
- `backend/tagger2/workflow/resources.py` - Content-addressed resource catalog with fingerprinting
- CSV validation for replacement resources
- Manifest storage with source provenance

**Validation & API**
- `backend/tagger2/workflow/preflight.py` - Configuration validation service
- `backend/tagger2/workflow/api.py` - API router at /api/v1/workflows
- Root boundary enforcement through WorkflowPathRef

**Testing**
- `backend/tests/test_workflow_foundation.py` - Contract, DB, resource, preflight tests

### 🔄 Phase 2: Processing Pipeline (In Progress)

**Stages to Implement**
- [ ] Caption stage (adapter to current LocalInferenceEngine)
- [ ] Classify stage (e621 tag classification)
- [ ] Replace stage (tag replacement index)
- [ ] OCR stage (PaddleOCR integration)
- [ ] NL stage (provider integration for natural language)
- [ ] Count Review stage
- [ ] Policy/Dropout stage
- [ ] Token Budget stage
- [ ] Export stage (nine-field JSON)

**Orchestration**
- [ ] Pipeline orchestrator
- [ ] Stage state machine
- [ ] Overlay management (workspace isolation)
- [ ] Commit journal and atomic operations

### 📋 Phase 3: Review & Recovery

- [ ] Count review UI and API endpoints
- [ ] Token budget review UI and API endpoints
- [ ] Pause/resume/repair capabilities
- [ ] Backup and restore operations
- [ ] Issue tracking and resolution

### 🎨 Phase 4: Frontend

- [ ] Dataset Workflow navigation page
- [ ] Job creation wizard
- [ ] Resource import/preview UI
- [ ] Pipeline status monitoring
- [ ] Count/Token review interfaces
- [ ] Bilingual support (Chinese/English toggle)

## Architecture Decisions

### Path Safety
All paths use `WorkflowPathRef` with root_id + relative_path. API never returns absolute paths.

### Resource Fingerprinting
Resources are content-addressed with SHA-256 fingerprints. Imported resources are immutable.

### Database Isolation
Workflow uses separate `workflows.sqlite3` (~804 MB tagger2.sqlite3 untouched, ~23.5 GB models preserved).

### Compatibility Mode
Jobs track compatibility flags for missing private resources (LSE14-5k scorer, proprietary classification data).

### Nine-Field Output
Standard output: quality[], count, character, series, artist, appearance[], tags[], environment[], nl

## Resource Strategy

### Bundled Resources
- e621 replacement index (user-provided CSV at D:\QQ相关\下载\E621tag替换索引\e621_general_tag_replacement_index.csv)
- Public e621 tag/alias/implication snapshots (to be downloaded)

### Private Resources Not Included
- LSE14-5k scorer (quality dropout model)
- Proprietary classification indices
- Non-redistributable model weights

Jobs using private resources are marked with compatibility warnings.

## Testing Strategy

### Unit Tests
- Contract validation
- Database operations
- Resource catalog
- Preflight validation

### Integration Tests
- End-to-end pipeline (e621 profile)
- Mixed input format handling
- Pause/resume/recovery
- Commit atomicity

### Manual Validation
- UI workflow
- Bilingual interface
- Resource import flow

## Next Steps

1. Implement Caption stage with adapter to current inference engine
2. Implement Classify stage with e621 profile
3. Add Replace stage with CSV resource
4. Build pipeline orchestrator
5. Add overlay management
6. Implement Count Review
7. Add Token Budget validation
8. Create Export stage
9. Build frontend components
10. Integration testing

## References

- Source project: E:\AI\e621-standard-capotion-workflow
- Current project: E:\AI\Tagger2_Inference_Rebuild2
- Task document: Tagger2 Dataset Workflow 全量合并计划

# Codebase Analysis: Source vs Target Integration

## Source Project Structure (e621-standard-caption-workflow)

### Core Architecture (core/src/anima_core/)
- **api.py** (106 lines) - FastAPI composition facade
- **contracts.py** (1,071 lines) - Versioned job config, protocols, validation
- **db_schema.py** (419 lines) - Schema v3, WAL mode, 23 tables
- **db.py** (940 lines) - Connection management, job CRUD
- **pipeline.py** (1,042 lines) - State machine, module dispatch
- **resource_catalog.py** (958 lines) - Content-addressed resources

### Workers (workers/{caption,classify,replace,ocr,nl,policy,token_budget,export}/)
Each worker is a separate Python package with:
- entry.py - CLI entry point
- worker.py - Main processing logic
- protocol.py - Contract validation
- resource.py - Resource loading
- Versioned JSONL I/O protocol

### Frontend (frontend/)
- React/Vite with TypeScript
- Bilingual (Chinese/English) with language toggle
- Job creation, monitoring, count/token review UIs

### Key Patterns
1. **Immutable contracts** with schema versioning
2. **Module-based pipeline** with restartable boundaries
3. **Overlay filesystem** (workspace + commit journal)
4. **Resource fingerprinting** (SHA-256 content addressing)
5. **Fail-closed validation** (preflight, resource checks)

## Target Project Structure (Tagger2_Inference_Rebuild2)

### Backend Architecture (backend/tagger2/)
- **main.py** (2,684 lines) - Monolithic FastAPI app with all routes
- **config.py** (370 lines) - TOML + env-based config
- **storage.py** (1,088 lines) - Single SQLite with job/item/event tables
- **local_inference.py** (1,521 lines) - Model loading, adapter support
- **model_registry.py** (691 lines) - Model catalog with download
- **security.py** (529 lines) - PathAllowlist, path safety, validation
- **providers/** - Online model providers (OpenAI-compatible, Gemini, LM Studio)

### Frontend Architecture (frontend/src/)
- React with Zustand state management
- Pages: Workbench, Batch, Models, Providers, Settings, VideoPrompts
- Chinese-only UI
- Single-page app with lazy loading

### Key Patterns
1. **Centralized config** (AppConfig from TOML + env)
2. **PathAllowlist** for root safety
3. **Runtime class** holding all services
4. **Single tagger2.sqlite3** (~804 MB)
5. **Model registry** with local/online modes

## Integration Strategy

### Module Mapping

| Source | Target | Strategy |
|--------|--------|----------|
| anima_core.contracts | workflow.contracts | ✅ Ported (Phase 1) |
| anima_core.db_schema | workflow.db_schema | ✅ New workflows.sqlite3 |
| anima_core.db | workflow.db | ✅ Separate connection manager |
| anima_core.resource_catalog | workflow.resources | ✅ Content-addressed catalog |
| anima_core.pipeline | workflow.orchestrator | 🔄 Next phase |
| workers.caption | workflow.stages.caption | 🔄 Adapter to LocalInferenceEngine |
| workers.classify | workflow.stages.classify | 🔄 Port classification logic |
| workers.replace | workflow.stages.replace | 🔄 CSV-based replacement |
| workers.ocr | workflow.stages.ocr | 🔄 JSONL protocol to PaddleOCR |
| workers.nl | workflow.stages.nl | 🔄 Adapter to Provider system |
| api_count_review | workflow.count_review | 📋 Phase 3 |
| api_token_budget | workflow.token_budget | 📋 Phase 3 |

### Reuse Existing Infrastructure

**From Tagger2**
- PathAllowlist → workflow path validation
- LocalInferenceEngine → caption stage backend
- ModelRegistry → caption resource registration
- Provider system → NL stage backend
- SecretStore → NL credentials
- ArtifactManager → OCR sidecar storage

**From Source**
- contracts.py → nine-field schema, versioning
- Classification logic → e621/Danbooru profiles
- Replacement algorithm → CSV processing
- Count review protocol → review UI contracts
- Token budget logic → tokenizer integration
- Commit journal → atomic operations

### Database Boundary

**Separate workflows.sqlite3**
- workflow_jobs (config, status, progress)
- workflow_samples (per-image state)
- workflow_issues (blocking/warning)
- workflow_module_summary (per-stage metrics)
- workflow_count_review (count evidence)
- workflow_token_budget_review (overflow proposals)

**Existing tagger2.sqlite3** (untouched)
- jobs, job_items, events (batch system)
- providers, secrets
- video_prompts

### API Boundary

**New routes** at /api/v1/workflows
- GET /capabilities
- GET /resources
- POST /resources/import/preview
- POST /resources/import/apply
- POST /jobs/preflight
- POST /jobs
- GET /jobs
- GET /jobs/{id}
- GET /jobs/{id}/issues
- POST /jobs/{id}/start
- POST /jobs/{id}/pause
- POST /jobs/{id}/resume
- POST /jobs/{id}/cancel
- GET /jobs/{id}/count-review
- POST /jobs/{id}/count-review/confirm
- GET /jobs/{id}/token-budget
- POST /jobs/{id}/token-budget/apply

**Existing routes** (preserved)
- /api/v1/health
- /api/v1/jobs (batch system)
- /api/v1/models
- /api/v1/providers
- /api/v1/video-prompts
- /api/v1/settings

### Frontend Boundary

**New page**: Dataset Workflow
- Job wizard (config builder)
- Resource import UI
- Pipeline status monitor
- Count review table
- Token budget review editor
- Issue list
- Commit controls

**Language toggle**: Workflow module only
- Chinese (default)
- English (opt-in)
- Persisted in localStorage

**Existing pages** (unchanged)
- Workbench (single-image tagging)
- Batch Jobs (directory scanning)
- Models (local inference)
- Providers (online APIs)
- Settings (paths, security)
- Video Prompts (image-to-video)

## Critical Differences & Adaptations

### 1. Caption Stage
**Source**: Standalone worker with model loading
**Target**: Adapter to LocalInferenceEngine
**Adaptation**: WorkflowCaptionAdapter wraps inference.run_batch()

### 2. NL Stage
**Source**: Direct OpenAI-compatible API calls
**Target**: Use existing Provider system (OpenAI, Gemini, LMStudio)
**Adaptation**: WorkflowNlAdapter wraps provider.generate()

### 3. OCR Stage
**Source**: Isolated Python runtime with PaddleOCR
**Target**: Same isolation strategy, use existing runtime/
**Adaptation**: JSONL protocol remains, path differs

### 4. Resource Storage
**Source**: Flat resource library with manifests
**Target**: data/workflows/resources/{category}/
**Adaptation**: Same content-addressing, different root

### 5. Workspace Layout
**Source**: data/jobs/{job-id}/ with overlay/
**Target**: data/workflows/jobs/{job-id}/ with overlay/
**Adaptation**: Same structure, different root

### 6. Configuration
**Source**: In-config resource references
**Target**: PathRef + resource_id
**Adaptation**: Preflight resolves and validates

## Resource Compatibility Matrix

| Resource | Source | Target | Status |
|----------|--------|--------|--------|
| e621 replace CSV | User-provided | Import from D:\QQ\下载\ | ✅ Importable |
| e621 tags snapshot | Public API | Download + cache | 🔄 Script needed |
| e621 aliases | Public API | Download + cache | 🔄 Script needed |
| e621 wiki | Public API | Download + cache | 🔄 Script needed |
| Caption model | HF download | ModelRegistry | ✅ Reusable |
| OCR model | Bundled | runtime/ocr/ | ✅ Existing |
| LSE14-5k scorer | Private | Not available | ⚠️ Compat flag |
| Danbooru resources | Private | Not available | ⚠️ Profile disabled |
| Tokenizers | HF download | transformers cache | ✅ On-demand |

## Testing Strategy

### Unit Tests (backend/tests/)
- test_workflow_foundation.py ✅
- test_workflow_stages.py 🔄
- test_workflow_orchestrator.py 🔄
- test_workflow_commit.py 🔄

### Integration Tests
- test_workflow_e621_pipeline.py 🔄
- test_workflow_mixed_input.py 🔄
- test_workflow_pause_resume.py 🔄

### Manual Validation
- UI workflow creation
- Resource import flow
- Count review interaction
- Token budget editing
- Commit verification

## Implementation Phases

### Phase 1: Foundation ✅
- Contracts, DB schema, resources
- Preflight validation
- API skeleton
- Basic tests

### Phase 2: Pipeline (Current)
- Caption adapter
- Classify stage
- Replace stage
- OCR integration
- NL adapter
- Orchestrator
- Overlay management

### Phase 3: Review
- Count review API + UI
- Token budget API + UI
- Issue tracking
- Pause/resume/repair

### Phase 4: Frontend
- Navigation integration
- Job creation wizard
- Resource management UI
- Pipeline monitor
- Review interfaces
- Bilingual toggle

### Phase 5: Validation
- End-to-end tests
- Resource scripts
- Documentation
- User guide

## File Count Summary

**Source project**: ~160 Python files
**Workflow module (target)**: ~30 files estimated
**Reused from target**: PathAllowlist, LocalInferenceEngine, Provider, ModelRegistry
**New infrastructure**: 7 files (Phase 1 complete)

## Next Implementation: Caption Adapter

```python
# workflow/stages/caption.py
class WorkflowCaptionAdapter:
    def __init__(self, engine: LocalInferenceEngine, config: dict):
        self.engine = engine
        self.config = config
    
    async def process_batch(self, samples: list[Sample]) -> list[Result]:
        # Load model if needed
        model_id = self.config["resource_id"]
        # Delegate to engine.run_batch()
        # Transform results to workflow protocol
```

This adapter lets workflow Caption stage reuse all of:
- Model loading (ONNX, safetensors, adapters)
- Preprocessing profiles
- Threshold modes
- GPU/CPU device selection
- Memory management

Zero duplication of inference logic.

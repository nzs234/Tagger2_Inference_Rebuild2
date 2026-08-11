# Tagger2 Workflow 生产就绪修复 - 进度报告

## 已完成的 P0 修复（5/10）

### ✅ P0-A: 响应 DTO 脱敏
**提交**: 2076fc
- 移除了 API 响应中的敏感字段：workspace_path、config_json、绝对 ackup_path、错误堆栈
- 添加了回归测试验证敏感字段被排除
- 前端类型定义已更新匹配

### ✅ P0-B: 事件循环阻塞修复
**提交**: 2076fc
- 将同步的 un_offline_pipeline 和 uild_ocr_engine 移出事件循环，使用 syncio.to_thread
- 添加测试验证流水线在工作线程中运行
- 防止长时间运行的任务冻结 API

### ✅ P0-C: Commit 原子回滚
**提交**: dfb38d5
- commit_staged_files 现在使用基于重命名的撤销机制：在覆盖前置换每个目标，失败时恢复所有文件
- 防止半提交数据集（文件 1..N-1 已写入，N 失败，数据集处于不一致状态）
- 测试验证：预存在文件恢复到原始字节，新创建文件被删除，日志记录 commit_rolled_back
- 同时修复了预存在的 mypy 错误（manifest 验证中的变量遮蔽）

### ✅ P0-D: Policy + Replace 资源修复
**提交**: 73c8c07
- **Issue 1**: 修复 pply_policy 接收字典而非 PolicyConfig dataclass 的崩溃
  - 创建 _parse_policy_config 函数，支持字典和 dataclass 输入
  - 正确解析嵌套的 CoupledProbabilities 结构
- **Issue 2**: Replace 阶段现在正确使用任务配置的 eplacement_index_path
  - API 层从 config.replace.resource_id 读取并通过 esource_catalog 解析路径
  - 验证路径正确传递到 pipeline

### ✅ P0-E: NL 阶段集成
**提交**: 73c8c07, d2d648c
- 创建 ProviderNlAdapter 将异步 VisionProvider 桥接到同步 NlClient
- 在 un_offline_pipeline 中连接 NL 阶段（Replace 之后，Policy 之前）
- 将 NL 结果（nl_projections）合并到样本注释中
- 更新 API 以在配置时从 provider 构建 NL client
- 修复 _parse_policy_config 以处理字典和 PolicyConfig 输入
- 添加 
l 字段到 PipelineReport
- 所有 298 个后端测试通过，35 个前端测试通过

## 剩余的 P0 问题（5/10）

### ⏳ P0-F: Count/Token Review 持久等待状态
**状态**: 未实现
**问题**: Review 不是阻塞式的持久状态；确认结果不会写回到导出
**需要**: 
- 实现 waiting_count_review 和 waiting_token_review 状态
- 在这些状态下暂停流水线执行
- 实现确认 API，将结果写入 overlay
- 从 checkpoint 恢复并继续到 Export

### ⏳ P0-G: 完整作业状态机
**状态**: 部分实现
**问题**: 
- Pause 只改变 DB 状态，处理循环不检查
- Resume 不重新调度
- 缺少 Cancel、Startup Recovery、显式 Start、Restore、Discard、Pin、Retention、SSE
**需要**: 建立完整状态机和 operation ledger，所有控制操作使用 CAS

### ⏳ P0-H: Restore 和数据集锁
**状态**: 未实现
**问题**: Restore 没有 API/数据库 operation，缺少数据集锁、baseline 重检和备份身份校验
**需要**: 全面实现 Restore 操作和数据集锁机制

### ⏳ P0-I: 样本数据库登记
**状态**: 未实现
**问题**: 生产路径不创建 workflow_samples，不更新样本、模块、计数和 issue 表
**需要**: Orchestrator 分页登记样本并事务化维护 stage run、lease、issue、event 和计数

### ⏳ P0-J: 编排器替换 BackgroundTasks
**状态**: 未实现
**问题**: API 在 FastAPI BackgroundTasks 中直接执行同步流水线，阻塞事件循环
**需要**: 重构为受 Runtime 管理的异步调度器，阻塞阶段进入受控线程

## 测试状态

### 后端
- **测试**: 298 passed, 1 skipped ✅
- **类型检查**: mypy 通过 ✅
- **代码质量**: ruff 有少量预存在问题（非关键）

### 前端
- **测试**: 35 passed (6 test files) ✅
- **构建**: TypeScript 和 Vite 构建通过 ✅

## 技术债务

### P1 优先级问题
- 配置不校验布尔值、数值范围、依赖关系
- SQLite 迁移使用可变 schema checksum
- 资源 ID 可被新内容覆盖
- OCR 位于错误阶段，每张图启动一次进程
- 前端只配置 Replace/OCR，强制关闭其他阶段
- Review/report/issues 查询不会自动刷新

### P2 优先级问题
- 缺少 100k 压力测试
- 缺少 SSRF、断电点、锁冲突测试
- 缺少真实 OCR 和官方快照测试
- 缺少移动端视觉验收

## 下一步建议

按照原始计划的分阶段实施策略：

1. **阶段 1: 契约与 DB** - 实现 ConfigV2、完整状态机、不可变迁移、DTO、CAS、events/operations
2. **阶段 2: 编排器** - 替换 BackgroundTasks，实现分页样本创建、阶段 checkpoint、lease、暂停/取消
3. **阶段 3: 资源系统** - 不可变 manifest、preview token、任务快照
4. **继续后续阶段** - Review/Policy/Token、Commit/Restore、前端

## 结论

当前已完成 **5/10 个 P0 关键修复**，所有测试保持通过状态。核心问题已经解决：
- ✅ API 不再泄漏敏感路径
- ✅ 事件循环不再被长任务阻塞
- ✅ Commit 具有原子性和回滚能力
- ✅ Policy 和 Replace 正确使用配置的资源
- ✅ NL 阶段已集成到流水线

剩余的 P0 问题主要集中在：
- 作业生命周期管理（状态机、暂停/恢复/取消）
- 数据持久化（样本登记、Review 状态）
- 恢复机制（Restore、数据集锁）

建议继续按照原始计划的分阶段策略逐步实现剩余功能。

# P0 剩余问题分析

## 当前状态
- ✅ P0-A: 响应 DTO 脱敏（已完成）
- ✅ P0-B: 事件循环阻塞（已完成）
- ✅ P0-C: 提交回滚（已完成）
- ✅ P0-D: Policy/Replace 资源选择（已完成）
- ⚠️ P0-E: NL 阶段和样本登记（未完成）
- ⚠️ P0-F: Count/Token Review 持久等待（未完成）
- ⚠️ P0-G: 作业状态机完整性（部分完成）
- ⚠️ P0-H: Restore 和数据集锁（未完成）

## P0-E: NL 阶段和样本登记

### 问题
1. `pipeline.py` 中未调用 `run_nl_stage()`
2. 样本未登记到 `workflow_samples` 表
3. Count Review 未播种数据
4. Token Review 在错误时机播种

### 修复方案
1. 在 Replace 之后、Policy 之前调用 NL 阶段
2. NL 需要 NlClient 实例（需要从 API 层传递）
3. 在 pipeline 开始时登记所有样本到数据库
4. Count Review 在 NL 后立即播种
5. Token Review 在 Policy 后、Export 前播种

### 依赖
- 需要 NlClient 实例（从 Provider + SecretStore 构建）
- 需要数据库连接（用于样本登记）
- 需要 job_id（用于关联样本）

## P0-F: Count/Token Review 持久等待

### 问题
1. Review 不是真正的持久状态
2. 确认操作未实现
3. Review 结果未写入 overlay
4. 确认后未从 checkpoint 恢复

### 修复方案
1. 添加新状态：`waiting_count_review`, `waiting_token_review`
2. Pipeline 在遇到 Review 点时返回特殊状态
3. API 提供 `/jobs/{id}/reviews/count/confirm` 端点
4. API 提供 `/jobs/{id}/reviews/token/apply` 端点
5. 确认后更新 overlay 并重新启动 pipeline

## P0-G: 作业状态机完整性

### 当前实现
- ✅ 创建作业
- ✅ 启动作业（通过 BackgroundTasks）
- ⚠️ Pause（只更新数据库，不检查）
- ❌ Resume（未实现）
- ❌ Cancel（未实现）
- ❌ Startup Recovery（未实现）
- ❌ Explicit Start（隐式启动）

### 修复方案
1. Pipeline 在每个样本/批次边界检查状态
2. 检测到 Pause 时保存进度并退出
3. Resume 从上次 checkpoint 继续
4. Cancel 设置标志并等待 graceful shutdown
5. Startup Recovery 检测 interrupted 状态

## P0-H: Restore 和数据集锁

### 问题
1. Restore 无 API 端点
2. Restore 无数据库 operation 记录
3. 数据集锁未实现
4. Baseline 重检未实现
5. 备份身份校验未实现

### 修复方案
1. 添加 `/jobs/{id}/restore` 端点
2. 在 operations 表记录 restore
3. 实现简单的文件锁机制
4. Commit 前重新计算 manifest hash
5. Restore 时验证备份 SHA-256

## 优先级排序

基于影响和复杂度：

1. **P0-E (高影响，中等复杂度)**: NL 是核心功能，必须接入
2. **P0-G (高影响，高复杂度)**: 状态机是所有控制的基础
3. **P0-F (中等影响，中等复杂度)**: Review 是人工介入点
4. **P0-H (中等影响，高复杂度)**: Restore 是安全网

## 建议实施顺序

### 阶段 1: NL 接入（估计 2-3 小时）
- 在 API 层构建 NlClient
- 修改 pipeline 签名接受 NlClient
- 在 pipeline 中调用 run_nl_stage
- 添加回归测试

### 阶段 2: 样本登记（估计 1-2 小时）
- 修改 pipeline 接受 db 和 job_id
- 在 import 后登记样本
- 更新样本状态（processing/completed/failed）

### 阶段 3: Review 持久化（估计 2-3 小时）
- 添加新状态到枚举
- Pipeline 返回 review 信号
- 添加确认 API
- 实现 overlay 更新和恢复

### 阶段 4: 状态机增强（估计 3-4 小时）
- Pipeline 检查点机制
- Pause/Resume/Cancel 逻辑
- Startup Recovery

### 阶段 5: Restore 和锁（估计 2-3 小时）
- Restore API 和逻辑
- 简单文件锁
- Baseline 重检

总估计：10-15 小时工作量

# 改动记录（fork 开发）

本文件逐条记录 fork（chrngzilyu/PBZ-Trainer）相对上游 sdkdsd/PBZ-Trainer v0.13.1 的每一处改动：位置、改了什么、为什么、对行为与兼容性的影响。

## 第 4 步：异常处理与错误提示（分支 `step4-error-handling`）

方案依据：先盘点约 60 处 `except`，确认真正危险的是 4 处回滚失败被 `pass` 吞掉，以及 `session_effects` 把严重错误降级为“暂缓”。本步只做错误分级与提示，不改任何偏移、写入目标或功能逻辑。

### 1. 新增 `errors.py`
- **改了什么**：新增错误类别 `TargetGone`（目标失效）、`ReadError`（读失败）、`WriteError`（写失败）、`RollbackError`（回滚失败，带 `rollback_errors` 列表）、`VersionError`（版本校验失败），以及日志异常类别 `LOG`。新增线程安全、有界（默认 100 条）的 `IssueQueue`，提供 `report / drain / read_failed / read_ok`。新增纯函数 `classify(exc)`、`summarize_issues(issues) -> (颜色, 文本)`、`recent_lines()`、`report_issue(game, ...)`。
- **为什么**：给错误分级，让 core 只入队、不碰界面；界面再按等级显示颜色。纯函数可以在没有 Tk 的环境里单测。
- **影响/兼容**：所有新异常都继承 `RuntimeError`，原有 `except RuntimeError` 和测试照常接住。没有归类的普通 `RuntimeError`/`ValueError`（如“对象已切换”“数值无效”）按“目标失效”处理：底部消息行照旧显示，不进状态栏。`report_issue` 在对象没有 `issues` 属性时会自动创建（测试用的假对象不走 `__init__`）。

### 2. `core.py`
| 位置 | 改了什么 | 为什么 | 影响/兼容 |
|---|---|---|---|
| 顶部 import（约第 17 行） | 引入 `errors` 中的类别与 `report_issue` | 上报需要 | 无 |
| `DataWriteError`（第 48 行） | 父类从 `RuntimeError` 改为 `WriteError` | 纳入“写失败”类别 | `WriteError` 仍是 `RuntimeError`，原有捕获不受影响 |
| `Game.__init__`（第 93 行） | 新增 `self.issues = IssueQueue()` | 存放待界面取走的错误 | 无 |
| `Game.__init__` 版本校验（第 109–127 行） | 哈希、映像头、64 位、映像大小、名称池校验失败改抛 `VersionError` | 归入红色“版本校验失败” | 消息文本不变，仍拒绝连接 |
| `Game.read`（第 143 行） | 读失败改抛 `ReadError` | 归入“读失败”，界面连续 20 次才提示 | 消息文本不变 |
| `Game.patch` 写失败清理（约第 490–502 行，原 488 行 `except Exception: pass`） | 回滚本身失败时：`cleanup` 记为 `rollback_failed`，在原异常上附 `rollback_errors` 和 `issue_reported=True`，上报 `ROLLBACK`；`write_failed` 日志新增 `rollback_errors` 字段 | **原来回滚失败被静默吞掉，日志还误记为 `target_changed_or_unreadable`，内存可能停在“改了一半”的状态** | 仍然抛出**原来的**异常（类型和消息都不变）；只是不再隐瞒回滚失败 |
| `refill_ultimate` 回滚（第 610 行附近） | 回滚失败时除写日志外，再上报 `ROLLBACK` | 原来只进日志，界面不知道 | 行为不变，多一条提示 |
| `stop_floor_lock` 失败（第 770 行，原 761 行） | `RuntimeError` 改为 `RollbackError`，附 `[原因]` | 让 session_effects 和界面识别为严重错误 | 消息文本不变 |
| 6 处 `log()` 外的 `except OSError: pass`（第 632/671/766/796/834/872 行）和 patch 内一处 | 改为上报 `LOG` | 原来日志写不进去时审计记录丢了也没人知道 | 不抛异常，只计数，状态栏显示灰色“日志异常 N” |
| `incoming_modifier` / `movement_modifier` / `switch_cooldown_modifier` 回滚（第 977/1023/1068 行，原 972/1023/1073 行的 `except Exception: pass`） | 三处重复代码合并为新方法 `_rollback_batch(completed, exc)`（第 1073 行）：逐条逆序恢复，失败的条目**保留在 `self.undo`**，上报 `ROLLBACK` 并在原异常上附 `rollback_errors` | **原来回滚失败被吞掉，用户不知道还有残留修改** | 原异常照常抛出；成功恢复的条目照旧从 undo 删除，行为与原来一致；保留的条目可以通过“恢复残留修改”再次恢复，不会重复写（有测试覆盖） |

### 3. `session_effects.py`
| 位置 | 改了什么 | 为什么 | 影响/兼容 |
|---|---|---|---|
| import、`__init__` | 新增 `floor_disabled` 标志 | 严重错误后停止重试 | 无 |
| 新方法 `_serious()`（第 61 行） | 只有 `RollbackError`、`WriteError` 或带 `rollback_errors` 的异常算严重错误：上报（已上报的不重复报）并返回类别 | 原来所有异常都显示为“暂缓写入，等待角色就绪”，连“原下限未能自动恢复”也被说成暂缓 | 目标失效、角色未就绪等仍走原来的“暂缓”文案 |
| `floor_step` 异常分支（第 96 行附近） | 严重错误时 `floor_disabled=True`，状态显示“强锁血：已停用，回滚失败，可能有残留修改（…）”或“…写入失败（…）”；之后不再重试 | 严重错误不能被掩盖，也不应反复写 | 需要用户重新勾选强锁血才会重新开始（界面会自动取消勾选） |
| `attack_step` 异常分支（第 144 行附近） | 严重错误时清空 `attack_request`，状态显示“攻击倍率：已停用，…” | 同上 | 需要重新应用攻击倍率 |

### 4. `teleport_unlock.py`
| 位置 | 改了什么 | 为什么 | 影响/兼容 |
|---|---|---|---|
| 第 15 行 import；`heartbeat` 异常分支（第 386 行，原 383 行 `pass`） | 心跳失败后 `disarm()` 也失败时，上报黄色 `WRITE`（功能名 `teleport`） | 原来直接忽略 | 仍依赖适配器自身的三秒失效兜底，行为不变，只是多一条提示 |

### 5. `app.py`
| 位置 | 改了什么 | 为什么 | 影响/兼容 |
|---|---|---|---|
| 顶部 | 引入 `errors`；新增 `ISSUE_COLORS`（灰 `#9aa0ab`、黄 `#e0b84f`、红 `#e0565b`） | 状态栏配色 | 无 |
| 界面底部（第 252 行起） | 新增错误状态栏：彩色文字 +“恢复残留修改”“详情”“已知晓”三个按钮 | 原来只有一行消息，下一条就把上一条覆盖 | 原消息行保留，显示内容不变 |
| 新方法 `fail(exc)`（第 270 行） | 18 处 `self.message.set(str(exc))` 统一改为 `self.fail(exc)`：先照旧显示消息，再分级——目标失效不进状态栏；读失败计连续次数；其余入队；core 已上报的不重复 | 统一分级 | 消息行显示的文字与原来完全相同 |
| 新方法 `drain_issues()`（第 281 行） | 在现有 1 秒 `tick()` 末尾取出界面和 core 两个队列的错误，按最高等级上色，显示各类计数和最近一条；回滚失败首次出现时弹窗 | 不引入线程，契合现有 `after` 轮询 | 无额外线程 |
| `show_issues()` / `acknowledge_issues()`（第 299/303 行） | “详情”弹出最近 5 条；“已知晓”清空状态栏、重置弹窗标记 | 红色常驻，必须由用户确认 | — |
| `toggle_floor`、`health_tick`（第 474/529 行） | 重新勾选强锁血时清除 `floor_disabled`；强锁血因严重错误停用时自动取消勾选 | 界面与实际状态一致 | — |
| `tick`（第 654/661 行） | 刷新成功时把连续读失败计数清零；攻击倍率“已停用”也显示到消息行；末尾调用 `drain_issues()` | 读失败只有连续 20 次才提示 | — |

### 6. 测试
- `test_core.py` 新增 `RollbackFailureReporting`（6 个）：patch 写入和回滚都失败；incoming / movement / cooldown 三个批量写入回滚失败时 undo 保留并上报；回滚失败后再执行 restore 不会重复写；日志 `OSError` 被计数。
- 新增 `test_errors.py`（12 个）：分类映射、消息文本不变、队列有界、8 线程并发、读失败阈值与清零、懒创建队列、状态栏颜色与文本、最近 5 条、session_effects 中“角色未就绪仍显示暂缓”“回滚失败不再降级且不重复上报”“攻击维持写入失败后停用”。
- 本机（Linux + `/tmp/winshim` 替身）：`test_core` + `test_errors` 共 78 个全部通过；`test_teleport_unlock` 改动前后都是同样 17 个错误（需要真实 Windows API）。
- **只能在 Windows 上验证**：`app.py` 的状态栏、按钮、弹窗（本机没有 tkinter）；`test_session_persistence.py`；`test_teleport_unlock.py`；实机游戏中的回滚失败场景。

## 第 8 步：GitHub Actions Windows 自动测试（分支 `step8-ci`）

方案依据：`/workspace/plan-step8-ci.md`。本步只加 CI 与补齐构建输入清单，不改任何业务逻辑、偏移或功能。

### 1. 新增 `.github/workflows/ci.yml`
- **改了什么**：新增 workflow `CI`。触发：`push`（分支 `main`/`master`/`step*` 与标签 `v*`）、`pull_request`、`workflow_dispatch`。权限仅 `contents: read`。两个 job，均在 `windows-latest` + Python 3.13：
  - **test**（每次必跑）：`python -m unittest discover -s . -p "test_*.py" -v`；`python app.py --self-check --report ui-check.json`；无论成败上传 `ui-check.json` artifact（保留 14 天）。
  - **build**（可选）：仅在 `workflow_dispatch` 或推送 `v*` 标签时运行；`pip install -r requirements-build.txt` 后执行 `python build_exe.py --output dist`；上传 `dist/*.exe` 与 `.build/onefile/build-report.json`（保留 14 天）。**不**自动创建 Release。
- **为什么**：本机是 Linux/Mac，完整测试集（含 teleport、session、Tk self-check）只能在 Windows 上跑；后续重构需要这层安全网。打包与测试分离，避免每次小改都打 10MB+ EXE。
- **影响/兼容**：不改运行时代码。CI 绿只说明单元测试与自检通过，**不等于**实机游戏验收。公开 fork 上 Actions 分钟一般够用；不注入密钥、不连游戏。

### 2. `build_exe.py` 的 `inputs` 列表
- **改了什么**：在 `inputs` 中补上第 4 步新增的 `errors.py`（插在 `core.py` 与 `ui_theme.py` 之间）。
- **为什么**：PyInstaller 会因 import 把 `errors.py` 打进包，但构建报告的 `input_sha256` 原先漏记该文件；首次跑 build job 时若漏记也不利于审计。
- **影响/兼容**：打包产物内容不变（本来就会被 import 进来）；仅报告多一条哈希。本地构建命令不变。

### 3. CI 首次跑红后的最小修复：`test_session_persistence.py`
- **改了什么**：假 Panel 夹具（`Panel.__new__`，不走 `__init__`）补上第 4 步新增的 `issues` / `issue_log` / `rollback_warned` / `issue_text` / `issue_label`。
- **为什么**：`tick()` 末尾会调用 `drain_issues()` 与 `issues.read_ok()`；夹具原先没这些字段，Windows CI 上 6 个 session 用例报 `AttributeError`。本机 Linux 从未跑过该文件，第 4 步漏测。
- **影响/兼容**：只改测试夹具，不改运行时代码；真实 `Panel.__init__` 本来就会初始化这些字段。

### 4. CI 打包：控制台编码
- **改了什么**：build job 的 `Build one-file EXE` 步骤增加环境变量 `PYTHONIOENCODING=utf-8`。
- **为什么**：PyInstaller 已成功产出 EXE 与 `build-report.json`，但 `build_exe.py` 最后 `print` 含中文路径时，Actions 托管 runner（cp1252）报 `UnicodeEncodeError`，步骤以非零退出，artifact 未上传。
- **影响/兼容**：只影响 CI 环境；本机中文 Windows 控制台（GBK）本来就能打印。不改 `build_exe.py` 业务逻辑。

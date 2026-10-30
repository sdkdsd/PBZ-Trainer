# 项目地图

Windows x64 外部修改器，当前适配版本与指纹在 profile.json。

- app.py：中文 Tk 面板及只读自检。
- ui_theme.py：原创深色主题、开关图像和提示。
- core.py：版本校验、玩家定位、数据写入日志、条件恢复。
- teleport_unlock.py：独立传送分派适配、三秒心跳及条件恢复；显式开启才分配和写入。
- test_teleport_unlock.py：测试进程自有 x64 代码、假内存和恢复边界；不打开游戏。
- session_effects.py：加载暂态暂停、当前玩家重绑定和攻击固定目标维护。
- test_session_persistence.py：场景切换与恢复请求回归。
- runtime_paths.py：内嵌资源路径与持久日志路径。
- test_core.py：假内存测试，不连接实际游戏。
- errors.py：错误分级、IssueQueue、状态栏汇总（第 4 步）。
- test_errors.py：错误分类与 session 严重错误回归。
- build_exe.py / build_exe.ps1：隔离环境构建单文件 EXE；inputs 须含全部会被报告哈希的源文件（含 errors.py）。
- .github/workflows/ci.yml：Windows + Python 3.13 CI（unittest、self-check；可选打包）。

命令：python -m unittest discover -s . -p 'test_*.py'（以 discover 实际用例数为准）；python app.py --self-check；python app.py --preview；./build_exe.ps1。CI 见 README「持续集成」；CI 绿不等于实机验收。

保留默认关闭、版本及对象身份校验、写前日志和条件恢复；不得写入旧对象地址或覆盖游戏后续改变的值。研究结果不能冒充实机验收。材料编辑、攻击倍速与异常防护不在当前范围；不要关闭用户正在运行的其他修改器。

0.13.1 仅更新界面与发布文案，保留既有功能逻辑和校验。穿行符已有记录且数量为 0 已由用户实机反馈可传送，原 6 个已恢复；从未获得、完全无记录未做实机验证。公开界面与新版介绍只显示项目版本，不加旧版标签；提示保留“会提示消耗穿行符，但实际不消耗”。传送分派恢复仍须核对 token 与当前值，已发布适配页不得在游戏运行时释放，不得捆绑第三方模组或完整游戏脚本。

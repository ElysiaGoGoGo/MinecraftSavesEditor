# MinecraftSavesEditor

> 离线 GUI 工具，用于读取和修改 **Minecraft Java Edition** 世界存档。
> 支持世界信息、方块/方块实体、玩家数据与物品栏、Curios 模组饰品栏、任意 NBT 数据；打开世界时自动识别版本。

An offline desktop GUI editor for Minecraft Java Edition saves: world data, block states, block entities, player data, inventories, Curios data, and arbitrary NBT, with automatic version detection.

---

## 功能

| 标签页 | 功能 |
|---|---|
| 世界信息 | 世界名称、版本/DataVersion、种子、时间、天气、出生点、难度、游戏规则；修改时间/天气/规则/出生点等 |
| 方块 / 方块实体 | 按坐标读取/设置方块（支持 `[properties]` 与负 Y）；读取/写入/删除方块实体；SNBT 自动多行格式化 |
| 方块搜索 | 按方块注册名反向查找当前维度所有已生成区块；最多统计 **32767** 个匹配，列表最多显示 **256** 个坐标；支持 `[properties]` 精确过滤、进度与取消 |
| 方块实体搜索 | 按 `block_entities[].id` 反向查找方块实体；同样上限 **32767 / 256**；双击结果定位并读取 |
| 玩家数据 | 玩家列表与生命/饥饿/经验/坐标/游戏模式修改；**物品栏编辑**（快捷栏、背包、盔甲、副手）；单人主机玩家同步到 `level.dat/Data.Player` |
| 模组数据 → Curios | 显示玩家 Curios 槽位组/槽位，编辑、新增、删除饰品；标注适用的 Curios / Minecraft / NeoForge 版本并警告版本偏差 |
| 原始 NBT | 对世界目录内任意 `.dat` 按 NBT 路径读取/写入 |
| 校验 / 备份 | 浅层/深度校验、整世界备份、备份目录管理 |

### 安全机制

- 打开世界时获取 `session.lock` 独占锁；游戏正在运行时拒绝读写；
- 覆盖文件前自动备份到 `<world>/.mctool_backups/<时间戳>/`；
- 使用临时文件 + 原子替换；
- 修改方块后移除相关光照/高度图缓存，交由游戏重新计算。

> **修改前请先完全退出游戏，并建议复制整份世界存档。**

## 环境要求

- **Python 3.10+**
- **Tkinter**

## 快速开始

```powershell
git clone https://github.com/ElysiaGoGoGo/MinecraftSavesEditor.git
cd MinecraftSavesEditor
python -m mc-save-editor
```

也可以启动时直接打开世界：

```powershell
python -m mc-save-editor --world "D:\saves\MyWorld"
```

> **注意**：仓库内的 Python 包目录名为 `mc-save-editor/`（带连字符），因此使用
> `python -m mc-save-editor` 启动。请从**仓库根目录**运行，不要先执行
> `cd mc-save-editor`；普通 `import mc-save-editor` 不是合法 Python 语法，如需在代码中
> 导入该包，建议后续改为合法包名（如 `mc_save_editor`）。

自检（不打开世界，启动后自动关闭）：

```powershell
python -m mc-save-editor --self-test --self-test-ms 800
```

## 版本支持

| 版本 | DataVersion | 状态 |
|---|---:|---|
| Minecraft 1.20.1 | 3465 | 兼容 |
| Minecraft 1.21.1 | 3955 | 已验证（主目标） |

- 打开世界时自动读取 `level.dat` 的 `Version.Name` / `DataVersion`；
- 受支持版本自动使用对应处理逻辑；
- 不受支持版本会弹出选择框，默认选中**版本号最近的受支持版本**；
- 工具不实现 DataFixer，不升级/降级存档；版本不一致时写入有风险，会先备份。

## Curios 支持

- 适用版本来自源码构建：**Curios 9.5.1+1.21.1 / Minecraft 1.21.1 / NeoForge 21.1.60+**；
- 数据路径：`playerdata/<uuid>.dat → neoforge:attachments → curios:inventory → Curios`；
- 仅编辑装备槽 `Stacks`；`Cosmetics` 等其它字段只读保留；
- Curios 加载时会校验槽位物品，放入不匹配的物品可能被游戏丢弃；修改前务必备份。

> **版本偏差警告：Curios / Minecraft / NeoForge 任一版本不一致都可能导致未定义行为。**

## 仓库结构

```text
MinecraftSavesEditor/
├─ .gitignore
├─ README.md
└─ mc-save-editor/          # Python 包（仅标准库）
   ├─ __init__.py
   ├─ __main__.py           # python -m mc-save-editor 入口
   ├─ nbt.py / region.py / palette.py / chunk.py
   ├─ level.py / player.py / items.py / curios.py
   ├─ search.py / versions.py / lock.py / world.py
   └─ gui.py                # Tkinter GUI（八标签页）
```

当前仓库只包含运行所需的 Python 源码；开发期的测试、打包脚本、第三方参考源码与测试世界不纳入版本库。

## 已知限制

- 不支持 LZ4 压缩区块（会提示改回 deflate）；
- 实体/POI/结构/进度/统计暂未封装高层编辑；
- 方块编辑不会同步 POI；
- 仅用于本地单人存档或离线服务端备份，请勿用于在线服务器或他人存档。

## 开发说明

本项目在 DeepSeek Harness 辅助下开发。

第三方名称归其各自所有者所有；本项目与 Mojang / Microsoft 无关。

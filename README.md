1. # FirmAtlas

   > 当前状态：需求分析与数据库设计阶段  
   > 文档版本：Design Baseline v0.4  
   > 本文档不是最终发布版 README，而是 FirmAtlas 当前可行方案的需求与数据库设计基线。

   ## 0x01 项目定义

   **FirmAtlas** 是一个面向个人漏洞猎人和合法安全研究的 IoT 固件发现与按需获取工具。它从国内外常见路由器与网络摄像头厂商的公开渠道中，自动发现并整理固件及其相关元数据，建立可检索、可追溯、可增量更新的固件目录。研究者可以浏览和筛选所需固件，并仅对选中的固件执行下载、校验与本地归档。

   FirmAtlas 的核心价值不是批量下载所有固件，而是先建立固件目录，再由研究者按需选择：

   ```mermaid
   flowchart LR
       A["发现固件"] --> B["整理元数据"]
       B --> C["建立本地目录"]
       C --> D["浏览与筛选"]
       D --> E["按需下载"]
       E --> F["校验与本地归档"]
   ```

   其中，“发现—整理—建立目录—浏览筛选”是默认工作流程；下载、校验和本地归档仅在用户明确选择固件后执行。

   ### 目标用户

   - 独立 IoT 漏洞猎人
   - 固件安全研究人员
   - 需要构建研究样本集的个人研究者
   - 需要通过 JSON 接入固件信息的安全研究工具

   ### 核心原则

   - **目录优先**：默认只采集元数据，不自动下载固件。
   - **按需获取**：只有用户明确选择后才下载固件文件。
   - **来源可追溯**：固件记录必须能追溯到厂商、地区和公开来源。
   - **地区隔离**：同一厂商的不同地区站点作为独立固件来源处理。
   - **保留历史**：厂商下架固件时标记为消失，不删除数据库记录。
   - **安全去重**：不根据单一型号或版本号跨地区合并数据。
   - **实现可替换**：业务逻辑依赖 Repository，不直接依赖 SQLite。
   - **适配器隔离**：适配器只负责发现、解析和刷新来源地址。
   - **面向个人研究**：默认配置保守，避免无必要的存储与网络消耗。

   ## 0x02 MVP 范围

   FirmAtlas MVP 需要让个人漏洞猎人通过统一 CLI，从 TP-Link 中国站和美国站的公开渠道中采集目标设备的固件元数据，建立可浏览、可筛选、可增量更新的本地目录，并按用户选择下载和校验指定固件。

   ### 支持的数据源

   MVP 只支持 TP-Link，但中国站和美国站分别适配：

   | 来源标识     | 厂商    | 地区 | 目标产品                   | 当前观察到的发现方式   |
   | ------------ | ------- | ---- | -------------------------- | ---------------------- |
   | `tp-link-cn` | TP-Link | CN   | 家用网络设备与 Tapo 摄像头 | 公开 API               |
   | `tp-link-us` | TP-Link | US   | 家用网络设备与 Tapo 摄像头 | 产品支持页面和下载链接 |

   API、HTML 页面和 `<a>` 标签只是固件信息的发现方式。发现下载地址不等于立即下载文件。

   ### 支持的产品类型

   这里的“路由器”采用面向家庭网络研究的宽泛定义，并非严格按照网络层设备类型划分。

   | 产品大类 | 标准类型       | 说明                       | 示例                  |
   | -------- | -------------- | -------------------------- | --------------------- |
   | `router` | `router`       | 有线路由器或无线路由器（含家用及企业级）| Archer / ER / NR / SAR 系列 |
   | `router` | `mesh_router`  | 家庭 Mesh 组网设备         | Deco 系列             |
   | `router` | `wireless_ap`  | 家用或小型网络无线接入点   | Wireless AP           |
   | `router` | `cellular_cpe` | 使用移动网络接入的路由设备 | 4G/5G CPE、移动路由器 |
   | `camera` | `camera`       | 网络摄像头                 | Tapo 摄像头           |

   Tapo 摄像头属于采集范围，但归类为 `camera`，不归类为 `router`。

   MVP 不采集：

   - 交换机
   - 无线网卡
   - 独立网络控制器
   - 智能插座、灯具、传感器和门锁
   - 与目标网络设备无关的其他智能家居产品

   厂商原始分类必须保留，但不能直接替代 FirmAtlas 的标准分类。无法可靠分类的候选产品不进入正式目录，应记录跳过原因。

   ### MVP 不包含

   - TP-Link 以外的厂商
   - TP-Link 中国站和美国站以外的地区
   - 图形界面或 Web 界面
   - 自动批量下载所有固件
   - CSV 导出
   - 定时后台采集
   - 分布式采集
   - 登录、验证码或复杂反爬绕过
   - 第三方固件镜像站
   - 固件解包和二进制分析
   - 漏洞扫描和相似度分析
   - 云端数据库或对象存储
   - 多用户与权限系统
   - 完整 HTTP 响应永久归档
   - 数据库自动迁移系统

   ## 0x03 用户工作流

   ### 建立固件目录

   1. 用户选择来源，例如 `tp-link-cn`。
   2. FirmAtlas 加载该来源的适配器。
   3. 适配器发现产品、硬件版本、固件发布和下载资源。
   4. 应用层完成校验、标准化、去重和持久化。
   5. 只有完整成功的来源采集才能执行消失对账。
   6. CLI 输出新增、更新、未变化、消失、跳过和失败数量。
   7. 默认不下载任何固件文件。

   ### 浏览与筛选

   1. 用户查询本地固件目录。
   2. 用户按照地区、产品类型、型号、硬件版本、固件版本、可见状态和下载状态筛选。
   3. CLI 默认输出表格。
   4. 用户可以选择 JSON 输出，供其他研究工具读取。

   ### 按需下载

   1. 用户从目录中选择一个或多个 `FirmwareArtifact`。
   2. FirmAtlas 创建下载记录。
   3. 下载器将数据写入临时文件。
   4. 如果地址失效，允许所属适配器刷新一次下载地址。
   5. 下载完成后计算 SHA-256，并比较可用的官方校验和。
   6. 校验成功或没有官方校验和时，将文件移动到最终目录。
   7. 数据库记录下载、校验、哈希和本地路径。

   ## 0x04 功能需求

   ### 1. 命令行界面

   CLI 必须支持：

   - 初始化数据目录和数据库
   - 查看全局和子命令帮助
   - 查看支持的数据源
   - 启动指定来源的采集
   - 查看采集运行历史和错误摘要
   - 浏览和组合筛选固件目录
   - 查看单个固件发布及其下载资源
   - 输出表格或 JSON
   - 下载用户选择的资源
   - 查看下载和校验历史
   - 查看当前有效配置

   ### 2. 地区来源独立适配

   `FirmwareSource` 表示某厂商面向特定地区提供的公开固件来源：

   ```text
   TP-Link
   ├── FirmwareSource: TP-Link China
   │   └── Adapter: tp-link-cn
   └── FirmwareSource: TP-Link United States
       └── Adapter: tp-link-us
   ```

   地区法规、无线电规范、语言、服务和市场策略可能导致同一设备在不同地区拥有不同固件。因此：

   - 中国站和美国站分别采集。
   - 每个来源拥有独立适配器。
   - 采集完整性按照来源判断。
   - 固件可见状态按照来源维护。
   - 一个来源中的固件消失不能影响另一个来源。
   - 相同型号、硬件版本和版本号不能作为跨地区合并依据。
   - 相同 SHA-256 只能证明文件内容相同，不能消除不同来源的目录记录。

   ### 3. 产品发现与分类

   每个产品至少保留：

   - 厂商和固件来源
   - 来源内稳定 `source_key`
   - 厂商原始产品名称
   - 原始型号和规范化型号
   - 产品系列
   - 产品大类和标准产品类型
   - 厂商原始分类
   - 产品来源地址
   - 首次发现和最后发现时间

   在 MVP 中，同一型号出现在不同地区时分别保存，不自动跨地区合并。

   ### 4. 硬件版本发现

   FirmAtlas 必须采集产品公开展示的全部硬件版本，例如：

   ```text
   Archer AX23 V1
   Archer AX23 V1.20
   Archer AX23 V2
   ```

   要求：

   - 不同硬件版本分别保存。
   - `V1`、`V1.20` 和 `V2` 不自动合并。
   - 原始值和规范化值同时保留。
   - 规范化值只用于查询，不作为自动合并依据。
   - 来源未提供硬件版本时使用 `unspecified`，不得猜测为 `V1`。

   未提供版本时使用：

   ```text
   source_key: __unspecified__
   raw_revision: null
   normalized_revision: unspecified
   revision_explicit: false
   ```

   ### 5. 固件发布与下载资源

   FirmAtlas 必须采集每个硬件版本在本次采集时仍公开展示的全部固件版本，而不只是最新版。

   “全部版本”仅指当前公开页面或 API 中仍可见的版本，不保证发现已经被厂商永久移除的历史版本。

   固件发布与实际文件必须分开：

   - `FirmwareRelease` 表示某硬件版本的一次固件发布。
   - `FirmwareArtifact` 表示该发布下实际可下载的固件文件。

   一个发布可以包含升级固件、恢复固件或其他固件包。发布说明本身不是固件文件。

   至少采集：

   - 固件原始版本和可选规范化版本
   - 发布日期、标题和说明（来源提供时）
   - 发布来源地址
   - Artifact 原始文件名和下载地址
   - Artifact 类型
   - 厂商声明的大小（来源提供时）
   - 官方校验和与算法（来源提供时）
   - 首次发现、最后发现和当前可见状态

   ### 6. 持久化目录

   MVP 使用 SQLite 保存固件来源、产品、硬件版本、固件发布、下载资源、采集运行和下载记录。

   SQLite 只保存结构化数据，不保存固件 BLOB。实际固件保存在本地文件系统，数据库保存相对路径、大小和哈希。

   默认采集：

   - 只保存元数据。
   - 不自动下载固件。
   - 已有记录不重复创建。
   - 新记录追加到目录。
   - 已有记录更新非身份字段。
   - 保留首次发现时间并更新最后发现时间。

   ### 7. 消失判定

   固件发布和 Artifact 至少具有：

   | 状态          | 含义                                       |
   | ------------- | ------------------------------------------ |
   | `active`      | 最近一次完整成功采集时仍然可见             |
   | `disappeared` | 以前可见，但最近一次完整成功采集时已经消失 |

   只有一次来源级完整采集成功后，才能将未再次发现的记录标记为 `disappeared`。

   以下情况不得触发消失判定：

   - 网络请求失败或超时
   - API 临时错误
   - 页面解析失败
   - 采集任务被中断
   - 只处理了部分产品
   - 适配器无法确认结果完整

   固件消失时：

   - 不删除数据库记录。
   - 保存最后成功发现时间和当前确认消失时间。
   - 保留下载地址和来源信息。
   - 不删除已经下载的文件。

   固件重新出现时恢复为 `active` 并清空当前 `disappeared_at`。MVP 不保存多次消失和恢复的完整事件历史。

   ### 8. 表格与 JSON 输出

   CLI 至少支持按以下条件筛选：

   - 厂商
   - 地区
   - 固件来源
   - 产品大类和标准类型
   - 产品系列和型号
   - 硬件版本
   - 固件版本
   - 可见状态
   - 下载状态
   - 校验状态

   表格用于人工浏览，可以为了终端宽度截断显示字段，但不能改变底层数据。

   JSON 用于其他研究工具接入，必须：

   - 使用稳定字段名。
   - 包含 `schema_version`。
   - 缺失值使用 `null`，不得猜测填充。
   - 不包含 ANSI 控制字符。
   - 不混入日志、进度条或说明文字。
   - 结果写入标准输出，日志和错误写入标准错误。
   - 与相同筛选条件下的表格表示同一数据集合。

   MVP 不支持 CSV。

   ### 9. 下载与校验

   用户可以选择一个或多个 Artifact 下载。发现新固件不能自动创建下载。

   下载必须：

   - 使用临时文件。
   - 记录请求、开始和结束时间。
   - 记录实际下载地址和错误。
   - 更新已接收字节数。
   - 失败时不得标记为完成。
   - 下载完成后计算 SHA-256 和实际大小。
   - 成功后保存相对路径。

   厂商提供官方校验和时必须比较；不匹配时状态为 `mismatch`，文件不能正常归档。

   厂商未提供官方校验和时，仍计算 SHA-256，校验状态为 `not_available`，并允许归档。

   ### 10. 失效地址刷新

   下载遇到可能失效的地址时，允许所属适配器重新解析并刷新 Artifact 地址。

   刷新必须满足：

   - 只能调用原来源适配器。
   - 结果仍属于同一 Product、HardwareRevision 和 FirmwareRelease。
   - Artifact 的 `source_key` 不得改变。
   - 已知文件名、大小和官方校验和不得出现无解释冲突。
   - 每个下载记录最多自动刷新一次。
   - 刷新失败时报告下载失败，不删除目录记录。
   - 下载失败不能单独证明固件已经消失。

   建议触发刷新：

   - 下载返回 `403`、`404` 或 `410`。
   - 来源明确使用具有过期时间的地址。

   网络超时和临时服务端错误应先执行正常重试，而不是立即刷新地址。

   ### 11. 增量更新与去重

   对同一来源重复采集必须幂等：

   - 已有产品、硬件版本、发布和 Artifact 不重复创建。
   - 新版本只新增对应记录。
   - 已有记录更新最后发现时间。
   - 默认不下载任何文件。
   - 完整采集缺失的记录标记为 `disappeared`。

   身份层级：

   ```text
   FirmwareSource
   └── Product
       └── HardwareRevision
           └── FirmwareRelease
               └── FirmwareArtifact
   ```

   下载后可以通过 SHA-256 识别相同内容，但 MVP 不强制执行跨地区物理文件去重。

   ### 12. 错误与跳过

   以下情况必须进入采集运行的错误摘要或结构化日志，不能静默忽略：

   - 产品类型无法映射
   - 页面结构无法解析
   - 必要身份字段缺失
   - 下载 URL 无效
   - API 返回非预期结构
   - 单个产品采集失败
   - 校验和格式无法识别

   单个产品失败不能导致已经成功持久化的数据丢失；存在影响完整性的错误时，本次运行不得执行消失对账。

   ## 0x05 非功能需求

   ### 可靠性

   - 重复采集幂等。
   - 数据库写入受事务保护。
   - 消失对账和采集完成状态在同一事务边界提交。
   - 下载中断不产生正常归档记录。
   - 程序启动时能够识别遗留的 `running` 或 `downloading` 状态。
   - 不允许静默删除数据库或固件文件。

   ### 可扩展性

   - 新地区或厂商通过适配器接入。
   - 业务层不依赖页面结构和 SQLite。
   - 分类、查询、下载和存储逻辑不得复制到适配器。
   - 未来更换 PostgreSQL 时，不重写核心业务用例。

   ### 可测试性

   - 适配器测试默认使用固定 HTML/JSON fixture，不访问真实网站。
   - 真实网络测试与单元测试分离。
   - 覆盖多硬件版本、多固件版本、重复采集、消失、重新出现和部分失败。
   - Repository 可以用测试实现替换。
   - 下载测试覆盖成功、超时、中断、大小不符、校验失败和地址刷新。

   ### 网络礼貌与安全

   - 只采集公开来源。
   - 不绕过登录、验证码和访问控制。
   - 请求具有明确超时和有上限重试。
   - 按来源限制并发和速率。
   - 遵守 `Retry-After`。
   - 不信任厂商返回的本地文件名。
   - 不保存 Cookie、认证令牌和敏感请求内容。

   ## 0x06 领域模型

   | 术语               | 含义                             |
   | ------------------ | -------------------------------- |
   | `FirmwareSource`   | 厂商在特定地区提供的公开固件来源 |
   | `Product`          | 某个来源中的产品记录             |
   | `HardwareRevision` | 产品硬件版本或未标明版本         |
   | `FirmwareRelease`  | 某硬件版本的一次固件发布         |
   | `FirmwareArtifact` | 某次发布下实际可下载的固件文件   |
   | `CrawlRun`         | 一次来源采集运行                 |
   | `DownloadRecord`   | 一次下载尝试及校验结果           |

   核心关系：

   ```mermaid
   flowchart TD
       S["FirmwareSource"] --> P["Product"]
       P --> H["HardwareRevision"]
       H --> R["FirmwareRelease"]
       R --> A["FirmwareArtifact"]
       A --> D["DownloadRecord"]
       S --> C["CrawlRun"]
   ```

   领域约束：

   - 一个 Product 只属于一个 FirmwareSource。
   - 一个 HardwareRevision 只属于一个 Product。
   - 一个 FirmwareRelease 只属于一个 HardwareRevision。
   - 一个 FirmwareArtifact 只属于一个 FirmwareRelease。
   - 采集元数据不能自动触发下载。
   - 不完整采集不能触发消失对账。
   - 不同硬件版本不能自动合并。
   - SHA-256 相同不能消除来源目录记录。
   - 校验失败的文件不能正常归档。
   - 适配器不能访问 Repository、SQLite 或本地存储。

   ## 0x07 `source_key` 规则

   `source_key` 是一条记录在其来源和父实体范围内的稳定身份，不是数据库主键，也不是显示名称。

   ### 生成优先级

   1. 厂商 API 提供的稳定 ID。
   2. 厂商页面提供的稳定 slug、产品 ID 或资源 ID。
   3. 去除易变参数后的稳定 URL 路径。
   4. 对稳定字段组合生成带版本前缀的 SHA-256。

   示例：

   ```text
   api-id:123456
   url-path:home-networking/wifi-router/archer-ax23
   derived:v1:5dd61c...
   ```

   ### 各实体规则

   #### Product

   优先使用产品 ID，其次使用稳定页面路径，最后使用：

   ```text
   hash(FirmwareSource + 标准产品类型 + 规范化型号)
   ```

   #### HardwareRevision

   优先使用来源版本 ID，否则使用：

   ```text
   hash(Product source_key + 厂商原始硬件版本)
   ```

   未提供版本时固定为 `__unspecified__`。

   #### FirmwareRelease

   优先使用厂商固件记录 ID，否则使用：

   ```text
   hash(
       HardwareRevision source_key
       + 原始固件版本
       + 发布日期
       + 来源提供的稳定发布标识
   )
   ```

   不能只使用固件版本号，因为厂商可能重新发布相同版本号。

   #### FirmwareArtifact

   优先使用厂商资源 ID，否则使用：

   ```text
   hash(
       FirmwareRelease source_key
       + 去除易变查询参数后的下载路径
       + 原始文件名
       + Artifact 类型
   )
   ```

   临时令牌、签名和过期时间不得进入身份计算。

   ### 唯一范围

   ```text
   Product:             FirmwareSource + source_key
   HardwareRevision:    Product + source_key
   FirmwareRelease:     HardwareRevision + source_key
   FirmwareArtifact:    FirmwareRelease + source_key
   ```

   每个适配器必须为其 `source_key` 生成规则编写契约测试。生成规则发生变化时，必须显式评估历史数据兼容性，不能直接替换。

   ## 0x08 响应与 fixture 策略

   MVP 默认不永久保存所有 HTML/JSON 原始响应。

   采用以下策略：

   - 为每种典型页面和 API 响应保存经过裁剪、脱敏的测试 fixture。
   - fixture 不包含 Cookie、令牌、个人数据和无关内容。
   - 单元测试只使用 fixture，不依赖真实网站。
   - 内容哈希可以写入调试日志，用于判断响应是否变化，但不作为核心目录数据。
   - 后续可以增加可选 `--save-responses` 调试模式。
   - 调试响应保存在可清理缓存目录，不写入 SQLite，也不视为永久归档。

   目录建议：

   ```text
   tests/fixtures/
   ├── tp-link-cn/
   └── tp-link-us/
   
   data/cache/http/       # 可选、可清理的调试缓存
   ```

   ## 0x09 适配器与 Repository 边界

   ### 适配器职责

   适配器负责：

   - 使用应用层提供的 HTTP 访问组件访问公开来源
   - 发现产品、硬件版本、固件发布和 Artifact
   - 映射标准产品类型
   - 生成稳定 `source_key`
   - 返回原始字段和来源 URL
   - 报告跳过、错误和结果完整性
   - 在下载地址失效时刷新同一 Artifact 地址

   适配器不得：

   - 直接创建自己的 HTTP 客户端和网络策略
   - 访问 Repository 或 SQLite
   - 创建下载任务
   - 下载固件文件
   - 决定本地文件路径
   - 执行消失对账
   - 跨地区合并产品或固件

   ### Repository 职责

   Repository 负责：

   - 幂等保存和更新目录实体
   - 查询来源、目录、运行和下载记录
   - 隐藏 SQLAlchemy Core 和 SQLite
   - 将数据库行转换为领域对象或查询 DTO
   - 配合明确事务边界完成采集和对账

   Repository 不得向业务层暴露：

   - SQLAlchemy Connection、Row 或 Table
   - SQLite 连接和异常
   - SQL 字符串
   - 数据库持久化模型

   ### 接口签名决策

   README 只固定职责、依赖方向和不可违反的约束。Repository、适配器和事务管理的具体 Python 接口签名由本地编程 Agent 根据实际目录、数据类型和首个纵向切片提出。

   Agent 必须先输出接口设计供评审，不得在同一步直接生成全部实现。接口设计至少说明：

   - 同步与异步边界
   - 方法参数和返回类型
   - Candidate 与领域对象的区别
   - 完整性如何报告
   - 错误如何表示
   - 事务边界如何协调
   - Artifact 地址如何刷新

   ## 0x0A 技术栈

   | 类别           | 选择                | 说明                           |
   | -------------- | ------------------- | ------------------------------ |
   | Python         | 3.12                | 项目运行版本                   |
   | 项目与依赖管理 | uv                  | 创建环境、锁定依赖、运行命令   |
   | HTTP 客户端    | HTTPX `AsyncClient` | 异步采集、连接复用和流式下载   |
   | 数据库         | SQLite              | 单用户本地目录                 |
   | SQL 工具       | SQLAlchemy Core     | 表定义、查询和事务，不使用 ORM |
   | 数据库迁移     | 暂不引入            | 开发阶段数据库可重建           |

   约束：

   - 整个采集任务复用长期存在的 HTTPX AsyncClient。
   - 适配器通过注入的 HTTP 访问组件使用 HTTPX。
   - 领域对象使用普通 dataclass 或等价纯 Python 类型。
   - SQLAlchemy Table 定义只存在于基础设施层。
   - MVP 使用同步 SQLAlchemy Core；不引入异步数据库驱动。

   ## 0x0B SQLite 数据库设计

   MVP 使用 7 张表：

   | 表                   | 职责                       |
   | -------------------- | -------------------------- |
   | `firmware_sources`   | 厂商、地区和适配器来源     |
   | `products`           | 来源下的产品               |
   | `hardware_revisions` | 产品硬件版本               |
   | `firmware_releases`  | 固件发布                   |
   | `firmware_artifacts` | 实际下载资源               |
   | `crawl_runs`         | 采集运行及完整性           |
   | `download_records`   | 下载、校验、哈希和本地路径 |

   ### 总体关系

   ```mermaid
   erDiagram
       FIRMWARE_SOURCES ||--o{ PRODUCTS : publishes
       PRODUCTS ||--o{ HARDWARE_REVISIONS : has
       HARDWARE_REVISIONS ||--o{ FIRMWARE_RELEASES : receives
       FIRMWARE_RELEASES ||--o{ FIRMWARE_ARTIFACTS : contains
       FIRMWARE_ARTIFACTS ||--o{ DOWNLOAD_RECORDS : downloads
       FIRMWARE_SOURCES ||--o{ CRAWL_RUNS : runs
   ```

   ### 通用数据库规则

   - SQLite 启用 `PRAGMA foreign_keys = ON`。
   - 主键使用应用生成的不透明 TEXT ID。
   - 时间统一保存为 UTC RFC 3339 文本。
   - 发布日期使用 `YYYY-MM-DD` 文本，缺失时为 `NULL`。
   - 布尔值使用 INTEGER，并限制为 `0` 或 `1`。
   - 固件文件不存入数据库 BLOB。
   - 文件路径保存为相对数据根目录的路径。
   - 核心目录数据不执行常规业务硬删除。
   - 父子外键默认使用 `ON DELETE RESTRICT`。
   - JSON 字段只保存低频错误摘要，不承载核心查询字段。

   ### 1. `firmware_sources`

   | 字段               | 类型    | 约束             | 说明                       |
   | ------------------ | ------- | ---------------- | -------------------------- |
   | `id`               | TEXT    | PK               | 来源 ID                    |
   | `vendor_key`       | TEXT    | NOT NULL         | `tp-link`                  |
   | `vendor_name`      | TEXT    | NOT NULL         | `TP-Link`                  |
   | `source_key`       | TEXT    | NOT NULL, UNIQUE | `tp-link-cn`、`tp-link-us` |
   | `name`             | TEXT    | NOT NULL         | 来源显示名称               |
   | `region_code`      | TEXT    | NOT NULL         | `CN`、`US`                 |
   | `locale`           | TEXT    | NULL             | `zh-CN`、`en-US`           |
   | `base_url`         | TEXT    | NOT NULL         | 来源基础地址               |
   | `adapter_key`      | TEXT    | NOT NULL         | 适配器标识                 |
   | `discovery_method` | TEXT    | NOT NULL, CHECK  | `api`、`html`、`hybrid`    |
   | `enabled`          | INTEGER | NOT NULL, CHECK  | 是否启用                   |
   | `created_at`       | TEXT    | NOT NULL         | 创建时间                   |
   | `updated_at`       | TEXT    | NOT NULL         | 更新时间                   |

   ### 2. `products`

   | 字段               | 类型 | 约束            | 说明               |
   | ------------------ | ---- | --------------- | ------------------ |
   | `id`               | TEXT | PK              | 产品 ID            |
   | `source_id`        | TEXT | NOT NULL, FK    | 所属来源           |
   | `source_key`       | TEXT | NOT NULL        | 来源内稳定键       |
   | `display_name`     | TEXT | NOT NULL        | 厂商原始名称       |
   | `model_raw`        | TEXT | NOT NULL        | 原始型号           |
   | `model_normalized` | TEXT | NOT NULL        | 查询用型号         |
   | `series`           | TEXT | NULL            | 产品系列           |
   | `product_family`   | TEXT | NOT NULL, CHECK | `router`、`camera` |
   | `product_type`     | TEXT | NOT NULL, CHECK | 五种标准类型之一   |
   | `source_category`  | TEXT | NULL            | 厂商原始分类       |
   | `source_url`       | TEXT | NOT NULL        | 产品公开来源       |
   | `first_seen_at`    | TEXT | NOT NULL        | 首次发现           |
   | `last_seen_at`     | TEXT | NOT NULL        | 最后发现           |
   | `last_seen_run_id` | TEXT | NOT NULL, FK    | 最近观察运行       |
   | `created_at`       | TEXT | NOT NULL        | 创建时间           |
   | `updated_at`       | TEXT | NOT NULL        | 更新时间           |

   唯一约束：

   ```text
   UNIQUE(source_id, source_key)
   ```

   分类组合：

   ```text
   router -> router | mesh_router | wireless_ap | cellular_cpe
   camera -> camera
   ```

   ### 3. `hardware_revisions`

   | 字段                  | 类型    | 约束            | 说明             |
   | --------------------- | ------- | --------------- | ---------------- |
   | `id`                  | TEXT    | PK              | 硬件版本 ID      |
   | `product_id`          | TEXT    | NOT NULL, FK    | 所属产品         |
   | `source_key`          | TEXT    | NOT NULL        | 产品范围内稳定键 |
   | `raw_revision`        | TEXT    | NULL            | 厂商原始表示     |
   | `normalized_revision` | TEXT    | NOT NULL        | 查询用表示       |
   | `revision_explicit`   | INTEGER | NOT NULL, CHECK | 来源是否明确提供 |
   | `source_url`          | TEXT    | NULL            | 硬件版本来源     |
   | `first_seen_at`       | TEXT    | NOT NULL        | 首次发现         |
   | `last_seen_at`        | TEXT    | NOT NULL        | 最后发现         |
   | `last_seen_run_id`    | TEXT    | NOT NULL, FK    | 最近观察运行     |
   | `created_at`          | TEXT    | NOT NULL        | 创建时间         |
   | `updated_at`          | TEXT    | NOT NULL        | 更新时间         |

   唯一约束：

   ```text
   UNIQUE(product_id, source_key)
   ```

   ### 4. `firmware_releases`

   | 字段                   | 类型 | 约束            | 说明                    |
   | ---------------------- | ---- | --------------- | ----------------------- |
   | `id`                   | TEXT | PK              | 发布 ID                 |
   | `hardware_revision_id` | TEXT | NOT NULL, FK    | 所属硬件版本            |
   | `source_key`           | TEXT | NOT NULL        | 硬件版本范围内稳定键    |
   | `version_raw`          | TEXT | NOT NULL        | 原始版本                |
   | `version_normalized`   | TEXT | NULL            | 查询用版本              |
   | `release_date`         | TEXT | NULL            | 厂商发布日期            |
   | `title`                | TEXT | NULL            | 发布标题                |
   | `release_notes`        | TEXT | NULL            | 发布说明                |
   | `release_notes_url`    | TEXT | NULL            | 说明地址                |
   | `source_url`           | TEXT | NOT NULL        | 发布来源                |
   | `visibility_status`    | TEXT | NOT NULL, CHECK | `active`、`disappeared` |
   | `first_seen_at`        | TEXT | NOT NULL        | 首次发现                |
   | `last_seen_at`         | TEXT | NOT NULL        | 最后发现                |
   | `disappeared_at`       | TEXT | NULL            | 当前确认消失时间        |
   | `last_seen_run_id`     | TEXT | NOT NULL, FK    | 最近观察运行            |
   | `created_at`           | TEXT | NOT NULL        | 创建时间                |
   | `updated_at`           | TEXT | NOT NULL        | 更新时间                |

   唯一约束：

   ```text
   UNIQUE(hardware_revision_id, source_key)
   ```

   不能仅使用 `version_raw` 建立唯一约束。

   ### 5. `firmware_artifacts`

   | 字段                          | 类型    | 约束            | 说明                                     |
   | ----------------------------- | ------- | --------------- | ---------------------------------------- |
   | `id`                          | TEXT    | PK              | Artifact ID                              |
   | `release_id`                  | TEXT    | NOT NULL, FK    | 所属发布                                 |
   | `source_key`                  | TEXT    | NOT NULL        | 发布范围内稳定键                         |
   | `artifact_type`               | TEXT    | NOT NULL, CHECK | `firmware`、`recovery`、`other_firmware` |
   | `original_filename`           | TEXT    | NULL            | 厂商文件名                               |
   | `download_url`                | TEXT    | NOT NULL        | 当前下载地址                             |
   | `url_last_resolved_at`        | TEXT    | NOT NULL        | 最近解析时间                             |
   | `url_expires_at`              | TEXT    | NULL            | 可确定的过期时间                         |
   | `advertised_size`             | INTEGER | NULL            | 厂商声明大小                             |
   | `media_type`                  | TEXT    | NULL            | 来源声明类型                             |
   | `official_checksum_algorithm` | TEXT    | NULL            | 官方算法                                 |
   | `official_checksum_value`     | TEXT    | NULL            | 官方校验值                               |
   | `visibility_status`           | TEXT    | NOT NULL, CHECK | `active`、`disappeared`                  |
   | `first_seen_at`               | TEXT    | NOT NULL        | 首次发现                                 |
   | `last_seen_at`                | TEXT    | NOT NULL        | 最后发现                                 |
   | `disappeared_at`              | TEXT    | NULL            | 当前确认消失时间                         |
   | `last_seen_run_id`            | TEXT    | NOT NULL, FK    | 最近观察运行                             |
   | `created_at`                  | TEXT    | NOT NULL        | 创建时间                                 |
   | `updated_at`                  | TEXT    | NOT NULL        | 更新时间                                 |

   唯一约束：

   ```text
   UNIQUE(release_id, source_key)
   ```

   ### 6. `crawl_runs`

   | 字段                | 类型    | 约束                     | 说明                                                     |
   | ------------------- | ------- | ------------------------ | -------------------------------------------------------- |
   | `id`                | TEXT    | PK                       | 采集运行 ID                                              |
   | `source_id`         | TEXT    | NOT NULL, FK             | 采集来源                                                 |
   | `status`            | TEXT    | NOT NULL, CHECK          | `running`、`completed`、`partial`、`failed`、`cancelled` |
   | `is_complete`       | INTEGER | NOT NULL, CHECK          | 是否确认完整                                             |
   | `started_at`        | TEXT    | NOT NULL                 | 开始时间                                                 |
   | `finished_at`       | TEXT    | NULL                     | 结束时间                                                 |
   | `products_seen`     | INTEGER | NOT NULL, DEFAULT 0      | 产品数                                                   |
   | `releases_seen`     | INTEGER | NOT NULL, DEFAULT 0      | 发布数                                                   |
   | `artifacts_seen`    | INTEGER | NOT NULL, DEFAULT 0      | Artifact 数                                              |
   | `items_added`       | INTEGER | NOT NULL, DEFAULT 0      | 新增数                                                   |
   | `items_updated`     | INTEGER | NOT NULL, DEFAULT 0      | 更新数                                                   |
   | `items_disappeared` | INTEGER | NOT NULL, DEFAULT 0      | 消失数                                                   |
   | `items_skipped`     | INTEGER | NOT NULL, DEFAULT 0      | 跳过数                                                   |
   | `error_count`       | INTEGER | NOT NULL, DEFAULT 0      | 错误数                                                   |
   | `error_summary`     | TEXT    | NULL                     | 总体错误摘要                                             |
   | `issues_json`       | TEXT    | NOT NULL, DEFAULT `'[]'` | MVP 低频结构化问题摘要                                   |
   | `created_at`        | TEXT    | NOT NULL                 | 创建时间                                                 |

   约束：

   - `is_complete = 1` 时 `status` 必须为 `completed`。
   - `running` 时 `finished_at` 必须为 `NULL`。
   - 只有 `completed` 且 `is_complete = 1` 能触发消失对账。

   ### 7. `download_records`

   每行表示一次用户下载任务。内部 HTTP 重试仍属于同一 DownloadRecord。

   | 字段                      | 类型    | 约束                | 说明                                                         |
   | ------------------------- | ------- | ------------------- | ------------------------------------------------------------ |
   | `id`                      | TEXT    | PK                  | 下载记录 ID                                                  |
   | `artifact_id`             | TEXT    | NOT NULL, FK        | 目标 Artifact                                                |
   | `status`                  | TEXT    | NOT NULL, CHECK     | `queued`、`downloading`、`completed`、`failed`、`cancelled`、`interrupted` |
   | `verification_status`     | TEXT    | NOT NULL, CHECK     | `not_checked`、`not_available`、`verified`、`mismatch`       |
   | `requested_at`            | TEXT    | NOT NULL            | 请求时间                                                     |
   | `started_at`              | TEXT    | NULL                | 开始时间                                                     |
   | `finished_at`             | TEXT    | NULL                | 结束时间                                                     |
   | `resolved_url`            | TEXT    | NULL                | 实际使用地址                                                 |
   | `url_refresh_count`       | INTEGER | NOT NULL, DEFAULT 0 | 地址刷新次数，MVP 最大 1                                     |
   | `temporary_relative_path` | TEXT    | NULL                | 临时文件路径                                                 |
   | `final_relative_path`     | TEXT    | NULL                | 最终相对路径                                                 |
   | `bytes_received`          | INTEGER | NOT NULL, DEFAULT 0 | 接收字节数                                                   |
   | `size_bytes`              | INTEGER | NULL                | 最终实际大小                                                 |
   | `sha256`                  | TEXT    | NULL                | 本地 SHA-256                                                 |
   | `attempt_count`           | INTEGER | NOT NULL, DEFAULT 0 | HTTP 尝试次数                                                |
   | `http_etag`               | TEXT    | NULL                | 下载响应 ETag                                                |
   | `http_last_modified`      | TEXT    | NULL                | 下载响应 Last-Modified                                       |
   | `error_code`              | TEXT    | NULL                | 稳定错误代码                                                 |
   | `error_message`           | TEXT    | NULL                | 人类可读摘要                                                 |

   约束：

   - `completed` 必须具有 `final_relative_path`、`size_bytes` 和 `sha256`。
   - `mismatch` 不能对应正常完成归档。
   - `url_refresh_count` 只能为 `0` 或 `1`。
   - 同一个 Artifact 同时最多存在一个 `queued` 或 `downloading` 记录，使用部分唯一索引保证。

   ### 推荐索引

   | 索引                                                         | 用途           |
   | ------------------------------------------------------------ | -------------- |
   | `products(source_id, product_family, product_type)`          | 来源与类型筛选 |
   | `products(model_normalized)`                                 | 型号查询       |
   | `hardware_revisions(product_id, normalized_revision)`        | 硬件版本查询   |
   | `firmware_releases(hardware_revision_id, version_normalized)` | 固件版本查询   |
   | `firmware_releases(visibility_status, release_date)`         | 状态与日期筛选 |
   | `firmware_artifacts(release_id, visibility_status)`          | Artifact 查询  |
   | `crawl_runs(source_id, started_at)`                          | 采集历史       |
   | `download_records(artifact_id, requested_at)`                | 下载历史       |
   | `download_records(status, requested_at)`                     | 下载状态查询   |
   | `download_records(sha256)`                                   | 已下载内容查询 |

   ## 0x0C 数据库版本策略

   MVP 开发阶段暂不引入 Alembic 等数据库迁移工具。开发数据库视为可重建数据，表结构变化时允许删除开发数据库并重新采集。

   必须使用 SQLite 自带的版本字段：

   ```sql
   PRAGMA user_version;
   ```

   初始结构设置为：

   ```sql
   PRAGMA user_version = 1;
   ```

   程序启动时检查数据库版本：

   - 版本匹配时正常运行。
   - 版本不匹配时停止并给出明确提示。
   - 不得静默删除或修改数据库。
   - 测试数据库可以自动重建。
   - 固件文件不能因为数据库重建被自动删除。

   SQLAlchemy `create_all` 只用于创建缺失表，不作为结构迁移方案。

   满足任一条件时重新评估迁移工具：

   - FirmAtlas 首次公开发布。
   - 用户数据库已经具有长期保留价值。
   - 表结构修改不能接受删除重建。
   - 同时存在多个数据库版本。
   - 需要迁移到 PostgreSQL。
   - 项目开始多人协作。

   ## 0x0D 本地文件布局

   采用人类可浏览目录：

   ```text
   data/
   ├── firmatlas.db
   ├── firmware/
   │   └── tp-link/
   │       ├── cn/
   │       │   └── archer-ax23/
   │       │       └── v1/
   │       │           └── 1.2.0/
   │       │               └── a19f23cd__firmware.bin
   │       └── us/
   ├── tmp/
   │   └── downloads/
   │       └── <download-id>.part
   ├── cache/
   │   └── http/
   └── logs/
   ```

   最终路径组成：

   ```text
   厂商 / 地区 / 型号 / 硬件版本 / 固件版本 / 文件
   ```

   规则：

   - 文件名前添加短 Artifact ID，避免同名冲突。
   - 所有路径片段经过安全规范化。
   - 禁止绝对路径、`..` 和路径分隔符注入。
   - 不直接信任服务器文件名。
   - 未完成文件统一放在 `tmp/downloads`。
   - 校验后通过原子移动进入最终目录。
   - 数据库保存相对路径。
   - 数据库是目录状态的权威来源，文件夹结构用于人工浏览。

   ## 0x0E HTTP 默认策略

   | 配置                 |             默认值 |
   | -------------------- | -----------------: |
   | 元数据请求全局并发   |                  4 |
   | 单来源并发           |                  2 |
   | 单来源请求速率       |          每秒 1 次 |
   | 同时下载固件数       |                  2 |
   | 连接超时             |              10 秒 |
   | 普通页面读取超时     |              30 秒 |
   | 固件下载空闲读取超时 |              60 秒 |
   | 最大尝试次数         | 3 次，包含首次请求 |

   正常重试：

   - 连接失败
   - 临时 DNS 错误
   - 读取超时
   - HTTP `408`、`429`、`500`、`502`、`503`、`504`

   默认不重试：

   - HTTP `400`、`401`
   - 元数据请求中的 `403`、`404`、`410`

   Artifact 下载遇到 `403`、`404` 或 `410` 时，可以执行一次地址刷新。

   退避：

   - 第一次失败后约 1 秒。
   - 第二次失败后约 2 秒。
   - 加入少量随机抖动。
   - `429` 提供 `Retry-After` 时优先遵守来源指定时间。

   所有默认值允许通过配置覆盖，但不能配置为无限并发或无限重试。

   ## 0x0F CLI 设计

   采用扁平、易记的命令：

   | 命令                                  | 作用                   |
   | ------------------------------------- | ---------------------- |
   | `firmatlas init`                      | 初始化数据目录和数据库 |
   | `firmatlas sources`                   | 查看支持的数据源       |
   | `firmatlas crawl <source>`            | 采集指定来源           |
   | `firmatlas list`                      | 浏览和筛选固件目录     |
   | `firmatlas show <release-id>`         | 查看发布及其 Artifact  |
   | `firmatlas download <artifact-id>...` | 下载一个或多个资源     |
   | `firmatlas runs`                      | 查看采集运行历史       |
   | `firmatlas downloads`                 | 查看下载历史           |
   | `firmatlas config`                    | 查看当前有效配置       |

   示例：

   ```bash
   firmatlas init
   firmatlas sources
   firmatlas crawl tp-link-cn
   
   firmatlas list \
       --source tp-link-cn \
       --type router \
       --model "Archer AX23"
   
   firmatlas show <release-id> --format json
   firmatlas download <artifact-id>
   firmatlas runs --source tp-link-cn
   firmatlas downloads --status failed
   ```

   `list` 建议支持：

   ```text
   --vendor
   --source
   --region
   --family
   --type
   --series
   --model
   --hardware
   --version
   --visibility
   --download-status
   --verification-status
   --limit
   --offset
   --format table|json
   ```

   全局参数：

   ```text
   --data-dir PATH
   --config PATH
   --verbose
   --no-color
   --help
   --version
   ```

   MVP 的 `--format` 只支持 `table` 和 `json`。

   ## 0x10 MVP 验收标准

   | 编号  | 验收条件                                                     |
   | ----- | ------------------------------------------------------------ |
   | AC-01 | Python 3.12 环境中可以通过 uv 安装依赖并运行 CLI             |
   | AC-02 | 用户可以查看全局帮助和子命令帮助                             |
   | AC-03 | `firmatlas init` 可以初始化数据目录、7 张表和 `user_version` |
   | AC-04 | 系统能够独立运行 TP-Link CN 和 US 适配器                     |
   | AC-05 | 采集默认不下载任何固件                                       |
   | AC-06 | 系统能够识别五种目标产品类型                                 |
   | AC-07 | 交换机等非目标产品不会进入正式目录                           |
   | AC-08 | 无法分类的候选被记录并跳过                                   |
   | AC-09 | 产品的全部公开硬件版本被分别保存                             |
   | AC-10 | 每个硬件版本当前公开的全部固件版本被保存                     |
   | AC-11 | 不同地区、产品和硬件版本不会错误合并                         |
   | AC-12 | 每种实体的 `source_key` 生成规则具有契约测试                 |
   | AC-13 | 重复采集不会产生重复目录记录                                 |
   | AC-14 | 新版本只新增对应记录，不自动下载                             |
   | AC-15 | 完整采集后缺失固件标记为 `disappeared` 而非删除              |
   | AC-16 | 不完整或失败采集不能触发消失判定                             |
   | AC-17 | 已消失固件重新出现时恢复为 `active`                          |
   | AC-18 | 数据通过 SQLite 持久化，重启后仍可查询                       |
   | AC-19 | 业务层通过 Repository 使用 SQLAlchemy Core，不直接依赖 SQLite |
   | AC-20 | 适配器不访问 Repository、数据库或文件存储                    |
   | AC-21 | 用户可以组合筛选固件目录                                     |
   | AC-22 | 相同查询支持表格和 JSON 输出                                 |
   | AC-23 | JSON 包含 `schema_version` 且不混入日志或 ANSI 字符          |
   | AC-24 | CLI 不提供 CSV 输出                                          |
   | AC-25 | 用户可以选择具体 Artifact 下载                               |
   | AC-26 | 下载完成后保存 SHA-256、实际大小和相对路径                   |
   | AC-27 | 有官方校验和时必须比较，失败文件不能正常归档                 |
   | AC-28 | 无官方校验和时允许归档并标记 `not_available`                 |
   | AC-29 | 失效下载地址最多自动刷新一次且不改变 Artifact 身份           |
   | AC-30 | 同一个 Artifact 不能同时存在两个活动下载任务                 |
   | AC-31 | 测试使用脱敏 fixture，不依赖真实网站                         |
   | AC-32 | 程序拒绝打开不兼容的 `PRAGMA user_version` 数据库且不静默删除数据 |

   ## 0x11 当前开发状态

   项目已于 2026-07-20 完成 MVP 实现与正式验收，AC-01～AC-32 全部通过。
   当前可通过 uv 安装和构建，并提供 TP-Link CN/US 元数据采集、SQLite 目录查询、
   消失对账、按需下载、校验及原子归档能力。

   详细的逐项证据、验证命令和非阻塞维护项见
   [`docs/mvp-acceptance.md`](docs/mvp-acceptance.md)。

   MVP 后的优先优化方向：

   1. 为 TP-Link US 型号抓取增加受配置约束的并发，缩短全量采集时间。
   2. 以独立机械提交统一现有 Python 文件的 Ruff 格式。
   3. 按需扩展 TP-Link CN 旧式固件标题解析，回补早期历史记录。

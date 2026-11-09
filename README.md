# filter：多视频题目批量初筛

```sh
git clone https://github.com/Pron-lei/filter.git
cd filter
python -m pip install -r requirements.txt
```

用于“scaling生成 → 模型初筛 → 人工复筛”。导入通用JSONL，输出通过题单、未通过原因和执行错误。只审核题面及媒体元数据，不上传完整音视频，不验证媒体事实或参考答案，pass不等于最终入库。

## 1. 安装与离线检查

需要Python 3.10+；Windows、Linux、macOS使用同一Python入口。唯一第三方依赖为jsonschema。下载ZIP并解压，或git clone仓库后，先进入filter目录：

```sh
python -m pip install -r requirements.txt
python run_prescreen.py --input examples/candidates.jsonl --mode dry-run --output runs/preflight
python run_prescreen.py --input examples/candidates.jsonl --mode mock --output runs/mock
```

示例为合成工程样例，没有绑定真实媒体；mock只验证接线，不能当作真实审核结果。

## 2. 配置API并先跑小批

真实调用需提供服务商地址、模型名及认证方式。默认不绑定供应商，不默认发送思考、JSON模式等可选参数。

PowerShell中隐藏输入密钥：

```powershell
$filterKey = Read-Host 'API key' -AsSecureString
$env:SCREENING_API_KEY = [System.Net.NetworkCredential]::new('', $filterKey).Password
python run_prescreen.py --input scaling.jsonl --mode live --base-url https://YOUR-PROVIDER/v1 --model YOUR-MODEL --max-tokens 8192 --limit 10 --output runs/small
```

把地址、模型名和输出上限改成服务商实际支持的值；上例只是参数模板。默认密钥变量SCREENING_API_KEY，也可用--api-key-env指定其它变量名。地址、模型可通过SCREENING_BASE_URL、SCREENING_MODEL设置。密钥不写入代码、题单或extra-body。

小批确认成功后，删除--limit并使用新的输出目录，开始完整批次。建议从并发2开始。按实际模型上限配置--max-tokens，当前默认24576；思考模型可能将推理token计入同一额度。

## 3. API兼容范围

| 接口                     | 支持范围                                | 常用配置                          |
| ------------------------ | --------------------------------------- | --------------------------------- |
| Chat Completions兼容接口 | 非流式及SSE流式，纯文本输入/输出        | 默认--api-format chat-completions |
| 原生Messages接口         | 非流式，文本块、stop_reason和usage映射  | --api-format anthropic-messages   |
| 本地Chat兼容服务         | localhost/127.0.0.1/::1回环HTTP、无认证 | --auth-style none                 |

支持以/v1等路径为base-url，也可直接填写完整/chat/completions或/messages端点，不会重复追加后缀。

可选参数：

- --stream：启用Chat兼容SSE；默认关闭。服务端返回普通JSON时可回退解析。
- --no-stream-usage：服务商不接受stream_options时省略它；未返回的用量不估算为实际费用。
- --response-format json-object：服务商支持JSON模式时显式启用，默认none；本地schema校验始终执行。
- --max-tokens-field max_completion_tokens：服务商要求该字段时替换max_tokens。
- --auth-style bearer / x-api-key / none：分别为Bearer、x-api-key请求头或不发送认证。
- --api-version：原生Messages版本请求头，默认2026-10-21；网关使用其它版本时按其文档指定。
- --extra-body extra.json：供应商扩展，如reasoning_effort或thinking。不得覆盖核心字段，不得在任何层级放认证密钥。
- --proxy-mode system / direct：跟随系统HTTP代理，或仅本进程绕过它。默认system；不修改Windows/VPN设置，不保证绕过系统级TUN。

本地无认证示例：

```sh
python run_prescreen.py --input scaling.jsonl --mode live --base-url http://127.0.0.1:8000/v1 --model LOCAL-MODEL --auth-style none --max-tokens 8192 --output runs/local
```

明确尚不直接支持：原生Responses、Gemini、Bedrock协议，原生Messages流式，签名认证或其它自定义认证头，带URL查询参数的端点，非回环地址的明文HTTP。相应服务提供兼容接口时可按上表接入，否则需要新增适配器。接口合同测试不等于每个供应商都已联网实测；换API/模型必须先做小批验证。

## 4. 输入：UTF-8 JSONL，一行一道题

```json
{"item_id":"q001","question":"根据视频1口述的规范核验视频2的操作，以下哪项描述正确？","options":{"A":"两项都满足","B":"仅顺序满足","C":"仅防护满足","D":"两项都不满足"},"capability":"C","subtask":"C4","media":[{"asset":"m001","duration_s":30,"tracks":["video","audio"],"file_available":null},{"asset":"m002","duration_s":45,"tracks":["video","audio"],"file_available":null}]}
```

- item_id整批唯一；重复ID的全部记录都作为输入错误，不擅自保留某一条。
- question和各选项支持字符串，或zh/en对象。当前要求A/B/C/D四个选项；其它题型需调整schema及导入器。
- capability/subtask为原标签，也接受original_capability/original_subtask。模型先独立预测，代码再比较；不把原标签、答案和人工评语发给模型。
- media按视频1、2…顺序排列，每条需asset或media_id。可提供与顺序一致的index。
- duration_s、tracks、file_available可省略或设null，表示未知；false才表示已知文件缺失。
- 提供本地path时只检查文件是否存在，不读取音视频内容。相对路径默认以题单目录为根，也可用--media-root指定。时长/轨道由生成端提供，不自动探测。
- parent_asset等后台关系用于辅助检查，不能默认替代题干中答题者可见的母本说明。
- reviewer_resources可说明tools、measurements；未知资源不自动当作缺陷。也支持--reviewer-resources resources.json。

字段结构见momni/prescreen/candidate.schema.json。输出目录必须与输入目录分离，避免覆盖源文件。原始记录可保留valid_answers等下游字段，模型输入采用白名单；通过题单仍完整保留这些原始字段。

## 5. 分流规则

| 顶层类型         | 含义                                                     |
| ---------------- | -------------------------------------------------------- |
| pass             | 基本满足当前初筛要求，继续媒体/人工审核                  |
| rework           | 单视频、能力不匹配、分类未定或有阻断问题；按原因分别处理 |
| processing_error | 输入或执行失败，final_decision为空；不能当作坏题         |

single严格不pass，淘汰补位，不投入改写。完整任务应核验多个来源时，不因局部选项编码差异把另一来源删掉；选项结构风险单独记录。母本比较缺少明确指代时，指出题面问题。

主能力和主子任务必须均匹配原标签；ambiguous/insufficient及题面uncertain目前汇入rework。疑似风险、未知工具和冗余输入应区分为非阻断提示。所有输入不必都必要，但核心任务需至少两个来源。

原模型输出保留；可验证的引文空白或页码格式问题允许校正，不能改文字/数字/词边界、预测标签或事实推导。引文可指向题干或选项中的待核验陈述，引用选项不等于认定选项为真。

## 6. 输出与续跑

- pass_candidates.jsonl：完整通过题单，交给下一阶段。
- rework_candidates.jsonl、rework_results.jsonl：未通过题及理由。
- processing_error_results.jsonl、input_errors.json：执行/输入错误及原行号。
- results.json、all_results.jsonl：完整模型返回、原标签、预测及分流依据。
- statistics.json、report.md、actions.json：统计、报告和淘汰补位/重标/复审/重试等动作。
- input_assignment：提示未分配为核心或冗余的输入，不自动把它们认定为冗余。
- preflight.json：dry-run检查，尚未做模型判断。
- items/、cache/、progress.jsonl、prompts/：逐题记录、成功缓存、进度和实际system/schema。

一个输出目录只运行一个进程。中断后重跑同一条命令：成功且有效的模型响应复用缓存，失败项重新请求。改模型、端点、prompt、原标签或生成参数会影响缓存；不要清空cache后再续跑。

默认并发2、有限重试；通过--timeout、--retries调整。鉴权/余额等全局错误会停止后续网络尝试；非法JSONL行单独报错。HTTP500、断流、无效JSON等不计为质量不合格。实际账单金额未知时为null。

当前输入及最终汇总使用线性内存，建议先按约1000题/文件分批，根据实际结果大小调整。待执行请求有界，完成后逐题落盘。完全重复覆盖全量；近重复只是候选提示，默认最多比较5000对，可用--near-pair-budget 0关闭。

## 7. 版本与核验范围

交接目录统一为filter，代码入口为momni.prescreen。默认通用prompt为joint_v4；v1/v2/v3可显式选择用于历史复现。业务规则可解释、实际system及schema有快照，不以复现某批人工保留数量为目标。

已做独立包启动、坏数据隔离、缓存续跑、1000条模拟记录，以及Chat/Messages合同测试。历史50题真实试跑32 pass、18 rework；这不是独立泛化准确率，仍有冗余列表漏填及分类分歧。通过题还需要媒体事实、答案与正式入库审核。

## 8. 一万题自动分批

建议500题/批（1万题分20批），批次顺序执行，批内并发由--concurrency控制，避免并发相乘。先用100题单批测吞吐/用量，不能把离线压力检查当作一万题真实API验证。

```sh
python run_batches.py --input scaling.jsonl --mode dry-run --batch-size 500 --output runs/large_preflight
python run_batches.py --input scaling.jsonl --mode live --batch-size 500 --base-url https://YOUR-PROVIDER/v1 --model YOUR-MODEL --max-tokens 8192 --concurrency 2 --output runs/large001
```

批量入口默认关闭近重复比较，跨批精确去重覆盖全量；重复item_id会在任何API调用前拒绝启动，必须先修复。其它坏行由对应批次单独报告。媒体相对路径仍以原题单目录为根。batch-size与source固定后，重跑同一命令会跳过已完成且无执行错误的批次；失败批次重新执行，但共享cache仍复用其已成功响应。

顶层batch_statistics.json/batch_report.md汇总数量、待处理数及本次API用量，pass_candidates.jsonl可交下游，processing_error_results.jsonl包含batch_id与原source_line。每批完整记录保留于batches/。最终合并逐行读取，一次只装载单批模型结果；不生成万题整批results.json，以免内存膨胀。

用上一次50题约26分钟的吞吐粗略线性外推，同配置1万题约86小时，实际取决于模型、并发、输出长度和服务稳定性；这不是运行时承诺。费用应依据学长自己的接口小批usage和费率预算。本程序不会自动扩并发或购买额度。

.env.example只是变量清单，程序不会自动加载.env；请用终端配置环境变量。GitHub仓库仅包含运行代码、规范摘录、合成样例和离线测试，不包含真实密钥、媒体、人工答卷或历史运行缓存。代码许可和规范公开范围由项目所有者另行确定，未自动添加开源授权。

# filter：多视频题目批量初筛

用于“scaling生成 → 模型初筛 → 人工复筛”。导入通用JSONL，输出通过题单、未通过原因和执行错误。只审核题面及媒体元数据，不上传完整音视频，不验证媒体事实或参考答案，pass不等于最终入库。

## 1. 安装和配置API

需要 Python 3.10+。下载ZIP并解压，或克隆仓库后进入filter目录：

```sh
git clone https://github.com/Pron-lei/filter.git
cd filter
python -m pip install -r requirements.txt
```

真实调用需提供服务商地址、模型名和认证方式。PowerShell中隐藏输入密钥：

```powershell
$filterKey = Read-Host 'API key' -AsSecureString
$env:SCREENING_API_KEY = [System.Net.NetworkCredential]::new('', $filterKey).Password
```

下面命令中的 `https://YOUR-PROVIDER/v1`、`YOUR-MODEL` 和 `8192` 是参数模板，需替换为服务商实际支持的地址、模型及输出token上限。默认密钥变量为SCREENING_API_KEY，也可用--api-key-env更换；地址和模型也可用SCREENING_BASE_URL、SCREENING_MODEL设置。密钥不写入代码、题单或extra-body。

**可以直接提交完整题单，不要求先做小批试跑。** 以下两种方式分开选择；两者都按题目发起请求，实际同时请求数由--concurrency控制，不是把全部题目塞进一个HTTP请求。

## 2. 大批次运行：整份题单单次执行

使用 `run_prescreen.py`，一次读取并处理整份JSONL，不加--limit，不自动拆成独立分片：

```sh
python run_prescreen.py --input scaling.jsonl --mode live --base-url https://YOUR-PROVIDER/v1 --model YOUR-MODEL --max-tokens 8192 --concurrency 2 --output runs/full001
```

- 直接处理完整题单，无需先跑10题或100题。
- 输入及完整结果在同一进程聚合，内存随题量和模型回复长度增长。
- 单题成功响应会缓存；中断后原样重跑同一命令，可复用已成功响应。
- 主要输出在runs/full001/：results.json、statistics.json、report.md、pass_candidates.jsonl等。
- --max-tokens当前程序默认24576，上例显式设为8192；请按实际模型限制配置，思考模型可能将推理token计入同一额度。

## 3. 分批次运行：自动拆分、续跑与汇总

使用 `run_batches.py`，仍然只提交一份完整题单，程序按--batch-size自动拆分，不需要手动切文件或逐批启动。以下以500题/批为例，1万题会分为20批：

```sh
python run_batches.py --input scaling.jsonl --mode live --batch-size 500 --base-url https://YOUR-PROVIDER/v1 --model YOUR-MODEL --max-tokens 8192 --concurrency 2 --output runs/batched001
```

- 批次顺序自动推进，每批内部并发2；可调整--batch-size和--concurrency。
- 每批独立保存完整结果，最终逐行合并，降低模型响应聚合时的内存压力。
- 完成且无执行错误的批次会跳过；失败批次重新执行，但共享cache仍复用其中已成功的响应。
- batch-size、输入和配置固定时，原样重跑同一命令即可续跑。
- 批量入口默认关闭近重复比较；跨批精确去重覆盖全量，重复item_id会在任何API调用前拒绝启动。其它坏行由对应批次单独报告。
- 媒体相对路径仍以原题单目录为根，不因分片文件位置改变。
- 顶层batch_statistics.json/batch_report.md汇总数量、待处理数和本次API用量；pass_candidates.jsonl交下游，processing_error_results.jsonl包含batch_id与原source_line。
- 每批完整响应保留在batches/；不生成万题整批的顶层results.json。

**整份单次运行与自动分批都可直接开始全量处理；分批是运行和恢复方式，不是“小批试跑”的前置要求。** 对1万题，可根据实际内存及运行管理需要选择自动分批。

## 4. 可选：离线检查或小批试跑

需要排查格式或接口配置时再使用，不是上述两种运行方式的必经步骤：

```sh
python run_prescreen.py --input examples/candidates.jsonl --mode dry-run --output runs/preflight
python run_prescreen.py --input examples/candidates.jsonl --mode mock --output runs/mock
python run_prescreen.py --input scaling.jsonl --mode live --base-url https://YOUR-PROVIDER/v1 --model YOUR-MODEL --max-tokens 8192 --limit 10 --output runs/optional_small
```

dry-run不调用模型；mock只验证工程接线，不是质量结论。示例为合成工程样例，没有绑定真实媒体，不能当作benchmark候选入库。

## 5. API兼容范围

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

明确尚不直接支持：原生Responses、Gemini、Bedrock协议，原生Messages流式，签名认证或其它自定义认证头，带URL查询参数的端点，非回环地址的明文HTTP。相应服务提供兼容接口时可按上表接入，否则需要新增适配器。接口合同测试不等于每个供应商都已联网实测；小批验证是可选的配置排查方式，不是全量运行的前置条件。

## 6. 输入：UTF-8 JSONL，一行一道题

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

## 7. 分流规则

| 顶层类型         | 含义                                                     |
| ---------------- | -------------------------------------------------------- |
| pass             | 基本满足当前初筛要求，继续媒体/人工审核                  |
| rework           | 单视频、能力不匹配、分类未定或有阻断问题；按原因分别处理 |
| processing_error | 输入或执行失败，final_decision为空；不能当作坏题         |

single严格不pass，淘汰补位，不投入改写。完整任务应核验多个来源时，不因局部选项编码差异把另一来源删掉；选项结构风险单独记录。母本比较缺少明确指代时，指出题面问题。

主能力和主子任务必须均匹配原标签；ambiguous/insufficient及题面uncertain目前汇入rework。疑似风险、未知工具和冗余输入应区分为非阻断提示。所有输入不必都必要，但核心任务需至少两个来源。

原模型输出保留；可验证的引文空白或页码格式问题允许校正，不能改文字/数字/词边界、预测标签或事实推导。引文可指向题干或选项中的待核验陈述，引用选项不等于认定选项为真。

## 8. 输出与续跑

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

整份单次入口的输入及结果聚合使用线性内存；自动分批入口则按分片处理并逐行合并结果。待执行请求有界，完成后逐题落盘。完全重复覆盖全量；近重复只是候选提示，默认最多比较5000对，可用--near-pair-budget 0关闭。

分批模式的顶层汇总为 `batch_statistics.json`、`batch_report.md` 和合并后的 JSONL；每批的 `results.json`、`statistics.json`、`report.md` 保留在 `batches/batch_xxxxx/`。分批模式不生成包含全部模型响应的顶层 `results.json`。

## 9. 版本与核验范围

交接目录统一为filter，代码入口为momni.prescreen。默认通用prompt为joint_v4；v1/v2/v3可显式选择用于历史复现。业务规则可解释、实际system及schema有快照，不以复现某批人工保留数量为目标。

已做独立包启动、坏数据隔离、缓存续跑、1000条模拟记录，以及Chat/Messages合同测试。历史50题真实试跑32 pass、18 rework；这不是独立泛化准确率，仍有冗余列表漏填及分类分歧。通过题还需要媒体事实、答案与正式入库审核。

## 10. 吞吐与仓库说明

用上一次50题约26分钟的吞吐粗略线性外推，同配置1万题约86小时，实际取决于模型、并发、输出长度和服务稳定性；这不是运行时承诺。费用应依据实际接口的usage和费率预算。本程序不会自动扩并发或购买额度。

.env.example只是变量清单，程序不会自动加载.env；请用终端配置环境变量。GitHub仓库仅包含运行代码、规范摘录、合成样例和离线测试，不包含真实密钥、媒体、人工答卷或历史运行缓存。代码许可和规范公开范围由项目所有者另行确定，未自动添加开源授权。

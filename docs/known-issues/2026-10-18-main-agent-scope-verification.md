# 主 Agent 业务范围调整与验证

日期：2026-10-18。范围：职责设计的业务受理边界；不包含追问生命周期、提交状态改造。

## 改动

- `app/prompts/agents/main.txt`：限定同城游玩服务；按事项受理混合请求；明确招呼、服务说明、行程解释和结果汇总；统一直接文本、finish_turn、追问和摘要转述的范围；查询失败不扩大职责。医疗问题仅简短转介，不附带诊疗判断。
- requirements、research、planning、revision 提示词：仅提取或处理业务内事项；完整原话保持真实，不透传越界内容。display 继续只按正文提取。
- `tests/scope_eval.py`：12 个场景及逐条语义评审口径，真实调用当前主模型和 TaskExecutionRuntime。专业能力被模拟为 unavailable，以验证分发和失败回退，不执行地图查询或真实规划，不连接正式存储。
- `tests/automated/test_scope_eval_contract.py`：离线验证评测器走通直接文本和 finish_turn 真实终止合同、无业务变更；不把模拟回复当成语义修复证据。

## 实际结果

模型 `qwen3.8:27b`，使用项目配置的 wire API 和主 Agent 调用参数。结果文件包含完整主提示词、SHA-256、每次用户输入、最终文本、工具参数和状态检查，可追溯每轮版本。

1. 有效基线：`.cache/scope_eval/before-valid-d4de1bb0.json`，数学、翻译两条均返回越界实质内容，运行合同正常。
2. 首轮调整：`.cache/scope_eval/after-7a65ea1a.json`，12 类各两次，共 24 次；数学、菜谱、翻译、诗歌、代码等不再提供实质答案，招呼、服务说明和业务内天气查询仍受理。发现医疗场景一次附带就医条件，随后收紧该规则。混合请求一次将 JSON 当作普通文本返回，内容未越界但展示不合格。
3. 最终版本：`.cache/scope_eval/final-structured-aa4d52d4.json`，6 类各两次，共 12 次，均观察到 finish_turn 完成；无越界实质答案。混合请求继续分发同城规划，天气继续分发查询，能力不可用后如实说明未知或未生成。
4. 最终医疗复测：`.cache/scope_eval/final-medical-recheck-2c0c2b66.json`，纯医疗及旅行夹带医疗各两次，共 4 次，均未提供剂量、症状清单或就医时机判断。该组既观察到直接文本，也观察到 finish_turn。
5. 上述最终 16 次均未选择业务候选，原需求、住宿及行程输入未被修改；未发现运行时异常。离线测试 2 项通过。

评测器调试中还有两份非验收材料：`before-6883135b.json` 请求代次构造错误导致终止失败，不用于基线统计；`final-medical-9c224cfc.json` 第四次结果因 FrozenDict 序列化中断未保留，修复评测器后重新完整执行四次。

## 复现

使用项目固定解释器：

```powershell
E:/anaconda/envs/TravelAssistant/python.exe -m pytest tests/automated -q
E:/anaconda/envs/TravelAssistant/python.exe -m tests.scope_eval
E:/anaconda/envs/TravelAssistant/python.exe -m tests.scope_eval --live --label repeat --repeat 2
E:/anaconda/envs/TravelAssistant/python.exe -m tests.scope_eval --live --label structured --structured --repeat 2
```

不带 `--live` 只列出场景，不发请求。带 `--live` 会向项目已配置模型端点发送提示词和合成输入；本次用户已明确授权真实评测。每次输出仍需按 rubric 人工阅读，未加入关键词判定器。

## 验证边界与剩余事项

此次调整是模型指令层的业务约束，沿用现有运行时的引用、条件和写入合同；没有添加语义分类器，不保证模型对所有改写都稳定遵守。16 次是最终版本的有限抽样，首轮其余场景未因单条医疗规则变动重复全量运行。

专业能力摘要透传及各专业 Agent 自身的语义行为已增加指令，但本次没有逐一真实运行这些 Agent；未验证恶意专业摘要注入、无上下文天气、多轮短答等全部设计场景。评测未覆盖真实业务候选成功提交，不代表整份职责设计验收完成。

个别旅行夹带医疗回复将每日时刻、偏好说成待补充，但这些字段本应可选，属于独立的需求追问问题。首轮原始 JSON 回复属于结束格式问题。二者均未在本次范围中修改。追问插话保留和保存状态冲突仍待后续处理。

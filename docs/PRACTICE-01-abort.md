# 练手任务 01：给对话加「停止生成」

> 用途：练「审查 AI 生成代码」的能力。把本文档的「需求 + 边界 + 验收」喂给 Cursor，
> 让它写；你负责跑、审、改。**不要直接看我折叠起来的提示**。

## 为什么选这个任务

中断流式请求是异步编程的经典坑。AI 生成的版本常见表现是：按钮点了、loading 停了，
但网络请求还在跑、token 还在计费、下一次发送行为诡异。它「看起来对」，但没真断。

## 需求

对话页（`web/src/components/ChatPanel.tsx`）在生成中时，「发送」按钮变成「停止」，
点击后：

1. **立即中断**正在进行的 SSE 流（不能等它自然结束）
2. **保留**已经生成的内容（不能清空）
3. 按钮恢复成「发送」，输入框可以马上用
4. **不弹错误提示**（用户主动取消不算错误）

## 边界要求

1. 连续点两次「停止」不能报错
2. 中断后立刻重新发送，新请求不受影响
3. 中断时正在累积的 `tool_calls` 不能污染下一次请求
4. 从对话 tab 切到其他 tab 时，如果正在生成，应当中断

## 验收清单（逐条勾，勾不上就是没做完）

- [ ] 生成中点「停止」，文字 **1 秒内**停止增长
- [ ] 停止后已生成的内容还在（没被清空、没变成占位符）
- [ ] 按钮变回「发送」，能立刻发下一条
- [ ] 连点两次「停止」不报错、不卡死
- [ ] 停止后立刻再发，回答完整正常
- [ ] 停止时**没有**红色错误 toast
- [ ] 浏览器 Network 面板里该请求状态是 `canceled`，不是 `200`（200 = 其实跑完了）

最后一条最能暴露问题：只改 UI 不改 fetch，请求会跑完，状态是 200。

## 不许改的地方

- `api.ts` 里 `streamSSE` 的**现有参数**不能变（可以加可选参数，比如 `signal`）
- 不能动 `agent` 模式的工具循环逻辑（`main.py`）
- 不能把流式改成非流式

## 提示（做完再看）

<details>
<summary>展开：AI 在这 6 处最常写错</summary>

1. **`fetch()` 没传 `signal`** —— 最常见。只创建了 `AbortController`、也调了
   `controller.abort()`，但 `api.ts` 里的 `fetch(..., { signal })` 根本没接。结果
   UI 停了、请求照跑。**验证方法：看 Network 面板状态码。**
2. **没有 `reader.cancel()`** —— 中断后读流的循环还挂在那儿，下次复用 reader 出问题。
3. **`catch` 里没区分 `AbortError`** —— 把用户主动取消当成失败，弹红色错误。
   `e.name === "AbortError"` 要单独处理并静默返回。
4. **`busy` 没在 `finally` 里复位** —— 按钮永久卡在「生成中」，只能刷新页面。
5. **复用了旧的 `AbortController`** —— 第二次发送时 controller 已经是 aborted 状态，
   请求一发出就被取消。每次发送必须新建。
6. **`acc` 没清空** —— 中断后残留文本，新回答接在旧答案后面。

</details>

## 做完之后，准备这三个答案（面试会问）

1. AI 第一次写的版本错在哪？（指出具体行）
2. 你是怎么发现的？（读代码 / 跑起来看 / Network 面板 / 加 console.log）
3. 你怎么改的？为什么这么改？

> 这三个答案比代码本身值钱。JD 里「能看懂、验证和修改 AI 生成的代码」考的就是这个。

## 参考实现（自己写完再看，不然白练）

<details>
<summary>展开参考答案</summary>

`api.ts` 加可选 signal：

```ts
export async function* streamSSE(
  path: string,
  body: unknown,
  signal?: AbortSignal,
): AsyncGenerator<StreamFrame> {
  const r = await fetch(path, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
    signal,                       // ← 关键：不传这个，abort 没有任何效果
  });
  ...
}
```

`ChatPanel` 里：

```ts
const ctrlRef = useRef<AbortController | null>(null);

async function send() {
  ...
  const ctrl = new AbortController();   // 每次新建
  ctrlRef.current = ctrl;
  try {
    for await (const f of streamSSE(path, body, ctrl.signal)) { ... }
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") {
      // 用户主动取消：静默，保留已生成内容
      return;
    }
    // 其他错误才提示
  } finally {
    ctrlRef.current = null;
    setBusy(false);                     // 必须在 finally
  }
}

function stop() {
  ctrlRef.current?.abort();             // 连点多次也安全（abort 幂等）
}
```

按钮：

```tsx
<button className="primary" onClick={busy ? stop : send}>
  {busy ? "停止" : "发送"}
</button>
```

切 tab 时中断（`App.tsx`）：

```tsx
useEffect(() => () => ctrlRef.current?.abort(), []);
```

</details>

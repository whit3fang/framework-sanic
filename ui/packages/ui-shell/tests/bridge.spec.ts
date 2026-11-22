import { describe, expect, it, vi } from "vitest";
import { dialpadRef, listenForCallContext, originAllowed, parseCallContext, readDevCallContext, readIdToken } from "../src/bridge/dialpad";

describe("dialpad bridge", () => {
  it("validates origins including wildcards", () => {
    const p = ["https://dialpad.com", "https://*.dialpad.com"];
    expect(originAllowed("https://dialpad.com", p)).toBe(true);
    expect(originAllowed("https://app.dialpad.com", p)).toBe(true);
    expect(originAllowed("https://dialpad.com.evil.io", p)).toBe(false);
    expect(originAllowed("https://evil.io", p)).toBe(false);
  });

  it("parses call context defensively", () => {
    expect(parseCallContext({ type: "call_context", payload: { call_id: 5, phone_number: "+1415" } })).toEqual({
      callId: "5", phoneNumber: "+1415", contactId: null,
    });
    expect(parseCallContext({ type: "something_else", call_id: 1 })).toBeNull();
    expect(parseCallContext("nope")).toBeNull();
    expect(dialpadRef({ callId: "1", phoneNumber: "+1", contactId: "9" })).toBe("contact:9");
  });

  it("ignores messages from unexpected origins", () => {
    const onCtx = vi.fn();
    const stop = listenForCallContext(["https://dialpad.com"], onCtx);
    window.dispatchEvent(new MessageEvent("message", { origin: "https://evil.io", data: { call_id: 1 } }));
    window.dispatchEvent(new MessageEvent("message", { origin: "https://dialpad.com", data: { call_id: 2 } }));
    stop();
    expect(onCtx).toHaveBeenCalledTimes(1);
    expect(onCtx.mock.calls[0]![0].callId).toBe("2");
  });

  it("reads and strips the id_token from the URL", () => {
    window.history.replaceState(null, "", "/app/?id_token=abc&x=1");
    expect(readIdToken()).toBe("abc");
    expect(window.location.search).toBe("?x=1");
  });

  it("only accepts URL call context in dev builds", () => {
    window.history.replaceState(null, "", "/app/?dev_call_id=42&dev_phone=%2B1415");
    expect(readDevCallContext(false)).toBeNull();
    expect(readDevCallContext(true)).toEqual({ callId: "42", phoneNumber: "+1415", contactId: null });
  });
});

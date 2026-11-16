import { describe, expect, it } from "vitest";
import { EVAL_FLAG_DEFAULTS, initialEvalFlags } from "@/lib/api/eval-config";

describe("scenario form flag defaults", () => {
  it("match the evaluation engine's defaults", () => {
    expect(EVAL_FLAG_DEFAULTS.exact).toEqual({ case_sensitive: true, normalize_whitespace: false });
    expect(EVAL_FLAG_DEFAULTS.keyword).toEqual({ case_sensitive: false });
  });

  it("start a new scenario from the defaults", () => {
    expect(initialEvalFlags(undefined, {})).toEqual({
      keywordCaseSensitive: false,
      exactCaseSensitive: true,
      exactNormalizeWhitespace: false,
    });
  });

  it("keep saved values when editing a scenario of the same type", () => {
    expect(
      initialEvalFlags("exact", {
        expected: "x",
        case_sensitive: false,
        normalize_whitespace: true,
      }),
    ).toMatchObject({
      exactCaseSensitive: false,
      exactNormalizeWhitespace: true,
    });
    expect(
      initialEvalFlags("keyword", { required: ["a"], case_sensitive: true }).keywordCaseSensitive,
    ).toBe(true);
  });

  it("apply the engine default when a saved exact config omits a flag", () => {
    // e.g. the demo seed's exact scenario stores only normalize_whitespace
    expect(initialEvalFlags("exact", { expected: "x", normalize_whitespace: true })).toMatchObject({
      exactCaseSensitive: true,
      exactNormalizeWhitespace: true,
    });
  });

  it("don't carry one type's flag over to another", () => {
    const fromKeyword = initialEvalFlags("keyword", { required: ["a"], case_sensitive: false });
    expect(fromKeyword.exactCaseSensitive).toBe(true);
    const fromExact = initialEvalFlags("exact", { expected: "x", case_sensitive: true });
    expect(fromExact.keywordCaseSensitive).toBe(false);
  });
});

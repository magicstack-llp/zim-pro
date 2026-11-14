import assert from "node:assert/strict";
import { test } from "node:test";
import { changedKeys, linkCitations, parseStart } from "../src/lib/helpers.ts";

test("linkCitations links prose citations while leaving inline and fenced code untouched", () => {
  const md = [
    "See [1] for details, and ignore [9].",
    "",
    "Use `items[1]` in JS.",
    "",
    "```python",
    "x = arr[1]",
    "```",
    "",
    "Back to prose [2].",
  ].join("\n");

  const expected = [
    "See [1](#cite-1) for details, and ignore [9].",
    "",
    "Use `items[1]` in JS.",
    "",
    "```python",
    "x = arr[1]",
    "```",
    "",
    "Back to prose [2](#cite-2).",
  ].join("\n");

  assert.equal(linkCitations(md, [1, 2]), expected);
});

test("changedKeys returns only modified fields", () => {
  const saved = { a: 1, b: "two", c: true };
  const form = { a: 1, b: "updated", c: false };
  assert.deepEqual(changedKeys(form, saved), { b: "updated", c: false });
  assert.deepEqual(changedKeys(saved, saved), {});
});

test("parseStart clamps invalid and negative values to 0", () => {
  assert.equal(parseStart(null), 0);
  assert.equal(parseStart(""), 0);
  assert.equal(parseStart("abc"), 0);
  assert.equal(parseStart("-5"), 0);
  assert.equal(parseStart("1.5"), 1);
  assert.equal(parseStart("20"), 20);
});

import assert from "node:assert/strict";
import { test } from "node:test";
import { tidyMarkdown as tidy } from "../src/lib/tidy.ts";

test("reasoning and control tokens are removed", () => {
  assert.equal(tidy("<think>the user wants water</think>\nBoil it."), "Boil it.");
  assert.equal(tidy("<think>still thinking about", { streaming: true }), "");
  assert.equal(tidy("Boil it.<|im_end|>"), "Boil it.");
  assert.equal(tidy("<thought>* Question: water\n* Plan</thought>Boil it."), "Boil it.");
});

test("an unclosed code block is closed, and code is left alone", () => {
  assert.equal(tidy("Run:\n```sh\necho ##hi [1,2] **x"), "Run:\n```sh\necho ##hi [1,2] **x\n```");
  assert.equal(tidy("```py\n#comment\nprint('**')\n```"), "```py\n#comment\nprint('**')\n```");
});

test("a whole answer wrapped in a markdown fence is unwrapped", () => {
  assert.equal(tidy("```markdown\n## Title\n\nText\n```"), "## Title\n\nText");
});

test("headings, bullets and numbered lists are repaired", () => {
  assert.equal(tidy("Intro\n##Methods\ntext"), "Intro\n\n## Methods\ntext");
  assert.equal(tidy("# Big title\n\ntext"), "## Big title\n\ntext");
  assert.equal(tidy("Steps:\n• one\n• two"), "Steps:\n\n- one\n- two");
  assert.equal(tidy("1) first\n2) second"), "1. first\n2. second");
  assert.equal(tidy("1.First\n2.Second"), "1. First\n2. Second");
  assert.equal(tidy("Pi is 3.14 exactly"), "Pi is 3.14 exactly");
  assert.equal(tidy("*item one\n*item two"), "* item one\n* item two");
  assert.equal(tidy("*emphasis* stays"), "*emphasis* stays");
});

test("bold markers are trimmed and unmatched ones settled", () => {
  assert.equal(tidy("** Filtration ** removes **sediment** and ** more **."), "**Filtration** removes **sediment** and **more**.");
  assert.equal(tidy("**a** and **b**"), "**a** and **b**");
  assert.equal(tidy("This is **important"), "This is important");
  assert.equal(tidy("This is **import", { streaming: true }), "This is **import**");
  assert.equal(tidy("Use `kubectl get", { streaming: true }), "Use `kubectl get`");
});

test("citation styles are normalised to [n]", () => {
  assert.equal(tidy("Boil it [1, 2]."), "Boil it [1][2].");
  assert.equal(tidy("Boil it [1 and 3]."), "Boil it [1][3].");
  assert.equal(tidy("Boil it [1-3]."), "Boil it [1][2][3].");
  assert.equal(tidy("Boil it【2】."), "Boil it[2].");
  assert.equal(tidy("Boil it [^2]."), "Boil it [2].");
  assert.equal(tidy("Boil it [Source 2]."), "Boil it [2].");
  assert.equal(tidy("Boil it (source: 1, 2)."), "Boil it [1][2].");
  assert.equal(tidy("Boil it<sup>3</sup>."), "Boil it[3].");
  assert.equal(tidy("In 1990-1995 [see notes]"), "In 1990-1995 [see notes]");
});

test("tables get a blank line and a separator row", () => {
  assert.equal(tidy("Compare:\n| A | B |\n| 1 | 2 |"), "Compare:\n\n| A | B |\n| --- | --- |\n| 1 | 2 |");
  const good = "| A | B |\n|---|---|\n| 1 | 2 |";
  assert.equal(tidy(good), good);
});

test("html, labels, escaped newlines and source lists", () => {
  assert.equal(tidy("Answer: Boil<br>it. <b>Now</b>."), "Boil\nit. **Now**.");
  assert.equal(tidy("Line one\\nLine two\\nLine three"), "Line one\nLine two\nLine three");
  const withList = "Boil it [1].\n\n**Sources:**\n- [1] Water purification\n- [2] Filters";
  assert.equal(tidy(withList, { hasSources: true }), "Boil it [1].");
  assert.equal(tidy(withList), withList.replace("**Sources:**\n", "**Sources:**\n\n"));
});

test("well-formed markdown is unchanged", () => {
  const good = "Boiling works [1].\n\n## Other methods\n\n- **Filtration** removes sediment [2].\n- Chlorine takes 30 minutes.\n\n1. Fill\n2. Boil\n\n```sh\necho done\n```";
  assert.equal(tidy(good), good);
});

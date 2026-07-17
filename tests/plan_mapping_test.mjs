import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import worker from "../workers/tongyi/src/worker.js";

const html = await (await worker.fetch(new Request("http://localhost"), {}, {})).text();
const parserSource = html.slice(
  html.indexOf("const PLAN_EXTRACT_MAX_HOURS_DEFAULT"),
  html.indexOf("function createEmptyWeeklySchedule"),
);
const extractPlanTextMapping = Function(`${parserSource}; return extractPlanTextMapping;`)();
const base = "自习室id：13476\n座位号:367\n";
const allDays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const fullWeeklyText = `自习室id:9908
座位号:001
时间段:
周一:8点到12点，12点半到16点半
周二:09:00-13:00,14:00-18:00
周三:09:00-13:00,14:00-18:00
周四:09:00-13:00,14:00-18:00
周五:09:00-13:00,14:00-18:00
周六:09:00-13:00,14:00-18:00
周日:09:00-13:00,14:00-18:00`;

for (const input of [
  "时间段:8:00-22:00",
  "时间段:8:00-12:00，12：00-18：00",
  "时间段:8:00-22:00，时间段:8:00-12:00，12：00-18：00",
]) {
  const plans = extractPlanTextMapping(base + input, { maxHoursPerObject: 0 });
  assert.deepEqual(plans.map(plan => plan.daysofweek), plans.map(() => allDays));
}

assert.deepEqual(
  extractPlanTextMapping(base + "周一:\n时间段:8:00-12:00", { maxHoursPerObject: 0 })[0].daysofweek,
  ["Monday"],
);

for (const [input, expected] of [
  ["时间段:8:00-12:00 18:00-22:00", [["08:00", "12:00"], ["18:00", "22:00"]]],
  ["时间段:8点到12点，8点半到12点半", [["08:00", "12:00"], ["08:30", "12:30"]]],
]) {
  const plans = extractPlanTextMapping(base + input, { maxHoursPerObject: 0 });
  assert.deepEqual(plans.map(plan => plan.times), expected);
  assert.deepEqual(plans.map(plan => plan.daysofweek), plans.map(() => allDays));
}

const mondayPlans = extractPlanTextMapping(
  base + "周一:8点到12点，8点半到12点半",
  { maxHoursPerObject: 0 },
);
assert.deepEqual(mondayPlans.map(plan => plan.times), [["08:00", "12:00"], ["08:30", "12:30"]]);
assert.deepEqual(mondayPlans.map(plan => plan.daysofweek), [["Monday"], ["Monday"]]);

const fullTongyiPlans = extractPlanTextMapping(fullWeeklyText, { maxHoursPerObject: 0 });
assert.equal(fullTongyiPlans.length, 14);
assert.deepEqual(fullTongyiPlans.slice(0, 2).map(plan => plan.times), [["08:00", "12:00"], ["12:30", "16:30"]]);

const appSource = await readFile(new URL("../qianduan/app.js", import.meta.url), "utf8");
const qianduanParserSource = appSource.slice(
  appSource.indexOf("function normalizeBulkTimeRange"),
  appSource.indexOf("function getExistingWeekSlotsForDay"),
);
const parsePlanTextSchedule = Function(`
  const DAY_OPTIONS = ${JSON.stringify(allDays)};
  const state = { planExtractMaxHours: 0 };
  ${qianduanParserSource}
  return parsePlanTextSchedule;
`)();

for (const input of [
  "时间段:19:00-22:00",
  "时间段：  \n  19：00-22：00",
  "时间段:8:00-12:00，时间段:12：00-18：00",
]) {
  const plan = parsePlanTextSchedule(input);
  assert.deepEqual(Object.keys(plan.dayTimes), allDays);
}

for (const [input, expected] of [
  ["时间段:8:00-12:00 18:00-22:00", ["08:00-12:00", "18:00-22:00"]],
  ["时间段:8点到12点，8点半到12点半", ["08:00-12:00", "08:30-12:30"]],
]) {
  const plan = parsePlanTextSchedule(input);
  for (const day of allDays) assert.deepEqual(plan.dayTimes[day], expected);
}

assert.deepEqual(
  parsePlanTextSchedule("周一:8点到12点，8点半到12点半").dayTimes,
  { Monday: ["08:00-12:00", "08:30-12:30"] },
);

const fullWeeklyPlan = parsePlanTextSchedule(fullWeeklyText);
assert.equal(Object.keys(fullWeeklyPlan.dayTimes).length, 7);
assert.deepEqual(fullWeeklyPlan.dayTimes.Monday, ["08:00-12:00", "12:30-16:30"]);
assert.deepEqual(fullWeeklyPlan.dayTimes.Sunday, ["09:00-13:00", "14:00-18:00"]);

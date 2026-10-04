# Evaluation: A and B

## Reports

| report | answered by | prompt | judge | recording | tools | golden set |
| --- | --- | --- | --- | --- | --- | --- |
| A | live (real) | cc8e3c33cbde | 0d63ecf60989 | none | same | same |
| B | live (real) | 586d7c78dba6 | 0d63ecf60989 | none | same | same |

## Pass rates

| grader | A passed | B passed | cases |
| --- | --- | --- | --- |
| citations | 40 | 39 | 40 |
| completed | 40 | 40 | 40 |
| cost | 40 | 40 | 40 |
| exclusion_clause | 40 | 39 | 40 |
| fraud_indicators | 40 | 40 | 40 |
| groundedness | 40 | 39 | 40 |
| human_oversight | 40 | 40 | 40 |
| latency | 40 | 40 | 40 |
| missing_documents | 40 | 40 | 40 |
| payable_amount | 40 | 39 | 40 |
| reason | 40 | 39 | 40 |
| recommendation | 38 | 38 | 40 |
| route | 40 | 40 | 40 |

## Differences

| case | what | A | B |
| --- | --- | --- | --- |
| CLM-0001 | observed judge_reason | `The statement aligns with the source's clause 3.1, which excludes damage due to ` | `The description provided specifies factors related to wear and tear, which align` |
| CLM-0001 | observed rationale | `The description indicates that the roof tiles were old and rotten prior to the s` | `The description specifies that 'The roof tiles were old and rotten before the st` |
| CLM-0003 | observed judge_reason | `The description specifies a sudden burst of a pipe, and neither clause excludes ` | `The source details a sudden burst pipe and states that exclusions relate only to` |
| CLM-0003 | observed rationale | `The description states a sudden burst of a pipe and water causing damage. No exc` | `The description states that a pipe 'burst' suddenly under the sink on 26 July 20` |
| CLM-0007 | observed judge_reason | `The source mentions that the exclusion applies to racing or similar activities, ` | `The incident described pertains to normal road usage and does not involve activi` |
| CLM-0007 | observed rationale | `The description does not mention any involvement in racing, rallies, or similar ` | `The description "I drove into the back of another car while driving my ?koda Oct` |
| CLM-0008 | observed judge_reason | `The source states exclusions for racing, driving under the influence, and unlice` | `The source description does not mention any of the exclusion conditions such as ` |
| CLM-0008 | observed rationale | `The description indicates a collision caused by a reversing van, and there are n` | `The description states "a van reversed into my Opel Astra while I was waiting at` |
| CLM-0009 | observed judge_reason | `The source text does not mention racing, intoxication, or unlicensed driving as ` | `The statement correctly interprets that the exclusions in the provided clauses d` |
| CLM-0009 | observed rationale | `The description does not indicate any of the exclusions apply, as there is no me` | `The description states, "a van reversed into my Dacia Duster while I was waiting` |
| CLM-0011 | observed judge_reason | `The description does not mention activities like racing or rallying that would i` | `The source specifies the exclusion criteria regarding races and similar activiti` |
| CLM-0011 | observed rationale | `The description does not indicate any participation in a race or similar activit` | `The description makes no mention of the insured vehicle participating in or bein` |
| CLM-0015 | observed judge_reason | `The statement correctly identifies that the exclusions listed are not applicable` | `The description and the clauses provided confirm that none of the exclusions (ra` |
| CLM-0015 | observed rationale | `The description does not mention facts that trigger any exclusions in the listed` | `The description states the car was hit in a car park and does not indicate any a` |
| CLM-0023 | observed judge_reason | `The source describes a sudden burst of a pipe and immediate water damage, which ` | `The source description indicates a sudden pipe burst on a specific date, which a` |
| CLM-0023 | observed rationale | `The description indicates a sudden burst of a pipe, resulting in immediate water` | `The description indicates "a pipe burst" on a specific date, suggesting a sudden` |
| CLM-0026 | observed judge_reason | `The source includes a clause (3.3) excluding coverage for collisions when the ve` | `The statement accurately interprets the source. Clause 3.3 explicitly excludes c` |
| CLM-0026 | observed rationale | `The description states that the car was driven by a friend without a driving lic` | `The description states, "A friend without a driving licence was at the wheel, be` |
| CLM-0031 | observed judge_reason | `The source describes the situation as a gradual leak from a dripping pipe, expli` | `The statement accurately refers to clause 3.4 in the source, which excludes cove` |
| CLM-0031 | observed rationale | `The description mentions a pipe that had been dripping and causing damage over m` | `The description mentions 'a pipe that was dripping' and 'The leak had been going` |
| CLM-0034 | grade groundedness | passed | failed |
| CLM-0034 | grade recommendation | failed | passed |
| CLM-0034 | observed assessment | `unavailable` | `none_applies` |
| CLM-0034 | observed groundedness | `no-rationale` | `ungrounded` |
| CLM-0034 | observed judge_reason | none | `The source does not explicitly confirm that the damage was not influenced by pri` |
| CLM-0034 | observed rationale | none | `The description attributes the damage directly to a severe storm on 6 May 2026, ` |
| CLM-0034 | observed recommendation | none | `approve` |
| CLM-0035 | observed judge_reason | `The source does not indicate any involvement in a race, rally, or related activi` | `The source does not indicate that the event described involved racing or other a` |
| CLM-0035 | observed rationale | `The description lacks any indication that the incident was related to a race or ` | `The description, "I drove into the back of another car while driving my Dacia Du` |
| CLM-0037 | observed judge_reason | `The source explicitly states in clause 3.2 that liabilities during timed session` | `The source clearly outlines the exclusion clause 3.2, which states liability is ` |
| CLM-0037 | observed rationale | `The description states that the incident occurred during a timed session on a tr` | `The description states "It happened during a track day, in a timed session for a` |
| CLM-0038 | grade citations | passed | failed |
| CLM-0038 | grade exclusion_clause | passed | failed |
| CLM-0038 | grade payable_amount | passed | failed |
| CLM-0038 | grade reason | passed | failed |
| CLM-0038 | grade recommendation | passed | failed |
| CLM-0038 | observed assessment | `applies` | `unavailable` |
| CLM-0038 | observed groundedness | `grounded` | `no-rationale` |
| CLM-0038 | observed judge_reason | `The claimant admits to consuming alcohol before driving, which aligns with the e` | none |
| CLM-0038 | observed payable_amount | none | `1820` |
| CLM-0038 | observed rationale | `The claimant described consuming alcohol before driving the vehicle, which align` | none |
| CLM-0038 | observed reason | `excluded` | `unverified` |
| CLM-0038 | observed recommendation | `reject` | none |

## Cost and latency

| report | model calls | input tokens | output tokens | cost EUR | median latency ms | max latency ms |
| --- | --- | --- | --- | --- | --- | --- |
| A | 14 | 9381 | 660 | 0.028609 | 848 | 2053 |
| B | 14 | 9619 | 844 | 0.031205 | 1019.5 | 2029 |

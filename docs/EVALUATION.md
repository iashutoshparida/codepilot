# Evaluation: `inventory_app`

Numbers below are produced by `codepilot evaluate`; rerun it to reproduce them.

| Metric | Original tests | Original + CodePilot tests |
|---|---|---|
| Tests | 3 | 169 |
| Line coverage | 33.9% | 95.8% |
| Branch coverage | 3.9% | 85.0% |
| Mutants killed | 7/90 | 79/90 |
| Mutation score | 7.8% | 87.8% |
| Injected bugs detected | 0/12 | 10/12 |

## Test generation

- Functions with generated tests: 19
- Tests generated: 168; kept after validation: 166; dropped: 2
  - dropped `test_average_order_value_properties` (behaviour-mismatch): ZeroDivisionError: division by zero Failing test case: test_average_order_value_properties( values=[], )
  - dropped `test_days_of_cover_properties` (behaviour-mismatch): ZeroDivisionError: division by zero Failing test case: test_days_of_cover_properties( level=0, # or any other 

## Edge-case findings (for human review)

- `inventory.reports:average_order_value([],)` raises `ZeroDivisionError`: division by zero
- `inventory.stock:days_of_cover(3, 0)` raises `ZeroDivisionError`: division by zero

## Injected bugs

| Bug | Description | Original | With CodePilot |
|---|---|---|---|
| B01 | bulk tier boundary moved: 100 units no longer gets 10% | missed | caught |
| B02 | mid tier discount typo: 50% instead of 5% | missed | caught |
| B03 | discount above 100% no longer rejected | missed | caught |
| B04 | tax and discount applied in the wrong order (changes rounding) | missed | missed |
| B05 | cannot sell the last unit in stock | missed | caught |
| B06 | reorder not triggered exactly at the reorder point | missed | caught |
| B07 | safety stock buffer scaled wrongly | missed | caught |
| B08 | zero quantity orders accepted | missed | caught |
| B09 | last partial box dropped from shipment | missed | caught |
| B10 | prime express threshold off by one | missed | caught |
| B11 | SKU check no longer normalises case and spaces | missed | missed |
| B12 | negative restock quantities accepted | missed | caught |

## Surviving mutants with generated tests (11)

- inventory/pricing.py:19 constant: 2 -> 3
- inventory/reports.py:8 constant: 2 -> 3
- inventory/reports.py:15 comparison: In -> NotIn
- inventory/reports.py:16 comparison: NotIn -> In
- inventory/reports.py:17 constant: 0 -> 1
- inventory/reports.py:17 constant: 0 -> 1
- inventory/reports.py:17 comparison: Gt -> GtE
- inventory/reports.py:19 constant: 0 -> 1
- inventory/reports.py:19 arithmetic: Add -> Sub
- inventory/reports.py:38 arithmetic: Add= -> Sub=
- inventory/reports.py:45 arithmetic: Add= -> Sub=

## Static analysis

9 findings: bare-except 1, deep-nesting 1, duplicate-code 2, mutable-default 1, unhandled-io 1, unsafe-call 1, unused-import 2

Run time: 141.8 s

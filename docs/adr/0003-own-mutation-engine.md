# ADR 0003: A small AST mutation engine instead of a third-party tool

**Status:** accepted

**Context.** Mutation testing is the evidence that generated tests are useful. Existing tools are heavier, configure the whole project, and are harder to drive programmatically per request.

**Decision.** Implement mutation with `ast.NodeTransformer`: arithmetic, comparison, logical, negation, integer-constant and return-value operators, one change per mutant, docstrings and `if __name__ == "__main__"` blocks skipped. Run the suite with `-x` on a disposable copy. A red baseline is refused.

**Consequences.** About 250 lines, easy to test and explain. `ast.unparse` drops comments in the mutated copy, which does not matter because the copy is temporary. Speed is one pytest process per mutant; incremental mutation on changed lines is future work.

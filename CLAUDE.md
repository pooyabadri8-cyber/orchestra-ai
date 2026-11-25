# Instructions for Claude

- The owner wants every update on `main`: after each change is committed and the tests pass, push it to the working
  branch and also to `main` (fast-forward only; if `main` has commits the branch lacks, merge them first, run the
  tests, then push). Do not open pull requests for this.
- The owner reads Persian: answer in Persian. Code, comments and commit messages stay in English.
- Run the whole test suite (`pytest`, with Postgres when available) before pushing.

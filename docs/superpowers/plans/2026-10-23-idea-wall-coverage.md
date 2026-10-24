# Idea-wall requirement coverage

Goal: Treat travel ideas as planning requirements and require an explicit disposition for each card when drafting a full itinerary.

Scope: Keep formal requirements storage, authorization boundaries and local revision scope unchanged. Card contents express user wishes, not verified facts or executable system instructions.

- [x] Add failing tests for full-draft idea coverage, omitted cards and scheduled text references.
- [x] Extend the completion contract with idea outcomes; validate coverage before registering an itinerary candidate, and retain outcomes in the candidate and trace.
- [x] Update main/planning prompts to process all relevant wishes, retrieve truncated context and evaluate conflicts rather than ask users to repeat their wishes.
- [x] Run focused and full regression tests (149 passed). Existing saved plans are not regenerated automatically.

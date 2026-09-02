# Exam Countdown Tool Specification

`getExamCountdown` is a read-only built-in tool for questions about upcoming
school examinations and the time remaining before them. It reads only the
fixed `school` branch of graph memory and returns raw JSON. The reply model
owns all natural-language phrasing, urgency, tone, and formatting.

## Output

The successful result has this shape:

```json
{
  "as_of_date": "YYYY-MM-DD",
  "exams": [
    {
      "subject": "subject name or null",
      "date": "date text copied from memory",
      "days_remaining": 3
    }
  ]
}
```

`days_remaining` is `null` when the date is not safe to normalise. An empty
School branch returns an empty `exams` list without a model call and is not an
error. Known past dates are omitted. Known dates sort soonest first, followed
by unknown dates.

## School read and extraction

`memory.school_context.read_school_branch()` walks the School subtree to the
graph traversal depth limit and returns a bounded snapshot of populated node
names, descriptions, and data. The seeded root description is taxonomy
metadata and is not treated as school content.

The tool sends that snapshot through the FAST tier as untrusted data. The
extractor returns only `subject`, the exact recorded date text, and an ISO
candidate. It is language-independent and contains no subject, examination,
or date word lists. The model is instructed not to infer missing years,
resolve relative phrases, or guess ambiguous dates.

Every returned date text must occur in the School snapshot; invented date
text is discarded. An invented subject is reduced to `null`. The ISO
candidate is advisory. The deterministic validator accepts it only when it
is a real ISO calendar date and the source date text contains the resulting
day and four-digit year as standalone numeric evidence. Otherwise the date
remains unknown. This conservative contract favours no countdown over a
plausible but incorrect one.

## Local day boundary

The tool resolves the user's timezone through the same
`get_location_context_with_timezone()` path used by reply context and
`getTime`. It falls back to the operating system timezone. Countdown
subtraction uses that local calendar date, never the UTC date. Tests inject
the current time and do not read the real clock.

## Failures

An extractor exception returns a stable retryable `unavailable` failure. A
malformed or empty model response is a successful empty result because the
tool has no reliable examination records to report. Debug logging records the
empty branch, extraction failure class, and result count without logging the
school facts.

"""One store per table. Each takes an `AsyncSession` and never commits: the service does.

Which clock a store stamps with (#268). A store takes the time from the application, as a `now`
argument, when its stamps are compared in Python or against a cutoff the caller also passes in, so
a test can move the clock: `conversations` (the flush claim, judged stale in the flusher) and
`reminders` (due and stale claims, one sender). A store uses the database's `now()` when the
deadline is compared in SQL against rows other processes write, because a stamp from one
process's clock would drift against the clock it is read with: `identities` (link expiry) and
`webhook_events` (leases and retry backoff). `assignments` stamps `notified_at` with `now()` in
the one statement that claims the ping, and only ever reads whether it is set.
"""

# Operator Requirements

The operator is the part of Jarvis that runs and supervises agent work: it starts runs on the
subscription providers, shows what they are doing, asks before anything irreversible happens, and
keeps a record. This document states what it has to do, not how.

## Provenance

Every requirement here is derived from two sources only: the owner's stated requirements, and
chapters 12 and 20 of [masterplan-subscription-rebuild.md](masterplan-subscription-rebuild.md),
whose provider capabilities were established by running the installed CLIs.

A third-party assistant project of the same name exists and solves a similar problem. No source
code, prompt, interface definition or asset from it has been read or paraphrased for this
document or for anything built from it. Observable product behaviour and public documentation
are the only admissible inspiration, and neither is quoted here. Implementation tickets cite this
document; they do not cite that project.

## Definitions

| Term | Meaning |
|---|---|
| Provider | A subscription-backed agent runtime Jarvis drives: Claude Code, Codex, or Hermes. |
| Session | A conversation thread held by a provider, identified by that provider's own id. |
| Run | One turn of work Jarvis started and is responsible for, from prompt to terminal status. |
| Foreign session | A session the user started outside Jarvis, visible but not under Jarvis's control. |
| Capability profile | The named set of permissions a run starts with. |
| Approval | A decision the user makes before a run may do something with a side effect. |

## Cross-cutting rules

These bind every requirement below.

- **R-1 Honest unknowns.** Where a provider offers no answer, the operator says so. It never
  substitutes an estimate, a remembered value or a plausible default for a fact it does not have.
- **R-2 No silent recovery.** Every failure ends in a state the user can see. Switching provider,
  retrying, downgrading a model or falling back to another route happens only on an explicit
  instruction or an explicitly enabled setting.
- **R-3 Attribution.** Anything the operator shows or stores names the provider, the model and the
  run it came from.
- **R-4 Default deny.** A run starts with the least capability that lets it begin, and gains more
  only through an approval the user gave for that run.
- **R-5 Local only.** The operator listens on the loopback interface and nowhere else. Nothing it
  exposes is reachable from another machine.

## A. Provider connections

| # | Requirement | Met when |
|---|---|---|
| OR-A1 | Show each provider's authentication state: logged in or not, the account, the plan, and how the login was made. | The three states are read from the provider's own status command, never inferred from a successful run. |
| OR-A2 | Start an interactive login for a provider on request. | The provider's own login flow opens in a visible terminal; the operator never handles a password or a token itself. |
| OR-A3 | Never fall back to a metered API key. | With `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` set in the environment, a run either uses the subscription or fails; it never bills an API account. |
| OR-A4 | Report each provider's reachability and round-trip latency. | A health view shows a per-provider result with its age, and probing does not consume quota when a real run has happened recently. |
| OR-A5 | Check the installed provider version against the minimum the adapter was written for. | A version below the minimum is reported as a named incompatibility with the installed and required versions, not as a runtime error. |
| OR-A6 | Show which tool servers and skills each provider currently exposes. | The list comes from the provider and distinguishes a server that failed to start from one that is absent. |

## B. Models

| # | Requirement | Met when |
|---|---|---|
| OR-B1 | List the models a provider offers, and record where each entry came from. | Every catalogue entry carries its source, and a provider with no listing interface starts with an empty catalogue rather than a guessed one. |
| OR-B2 | Let the user name a model a provider does not list, and verify it before use. | The name enters the catalogue only after a minimal run against it succeeds; a failed verification is reported with the provider's own error. |
| OR-B3 | Pin a model per run, per project and globally. | The most specific pin wins, and the run header shows which one applied. |
| OR-B4 | Never change the model of a running run. | A global model change takes effect on the next run and leaves running and scheduled work untouched. |
| OR-B5 | Record every model change. | The audit trail names the old model, the new model, the scope and who made the change. |

## C. Sessions

| # | Requirement | Met when |
|---|---|---|
| OR-C1 | List sessions per provider, including ones the user started elsewhere. | Foreign sessions are marked as such and carry the same metadata as owned ones. |
| OR-C2 | Show a session's history. | The turns are readable in order with their tool calls, without starting a run. |
| OR-C3 | Treat foreign sessions as read-only. | Steering, interrupting and resuming are unavailable on them until the user takes the session over, which forks it rather than writing into it. |
| OR-C4 | Resume an owned session. | The provider's own resume path is used, and the resumed run keeps the session's model unless the user changes it. |
| OR-C5 | Fork a session. | The fork starts from the chosen point and is independent: work in it never changes the original. |
| OR-C6 | Rename, archive and delete owned sessions. | Deletion is confirmed, states what will be lost, and is recorded. |

## D. Runs

| # | Requirement | Met when |
|---|---|---|
| OR-D1 | Start a run from a prompt, a provider, an optional model, a working directory and a capability profile. | A run that cannot be started reports which of those was rejected and by whom. |
| OR-D2 | Stream a run live: assistant text, reasoning where the provider supplies it, tool calls and their results. | The view updates as the provider emits, and it is clear whether a run is waiting on a tool, on the user, or on the provider. |
| OR-D3 | Send a message into a running run. | The message reaches the provider mid-run where the provider supports it, and where it does not, the operator says so instead of queueing it silently. |
| OR-D4 | Cancel a run. | The provider process and everything it started are gone, and the run ends as cancelled rather than as an error. |
| OR-D5 | End every run in exactly one terminal status: finished, failed, cancelled, timed out, or out of quota. | Out of quota is distinct from failed, and reaching it never starts the same work on another provider by itself. |
| OR-D6 | Keep a durable record of every run. | After a restart, a past run still shows its provider, model, profile, working directory, timing, token counts, origin and full event stream. |
| OR-D7 | Retry a finished run. | The retry is a new run that names the one it came from, so the two are never conflated. |
| OR-D8 | Surface a run that is waiting for the user. | The waiting run appears in one place across all providers, and the user can answer it from there. |
| OR-D9 | Bound a run. | Wall-clock time, output size and tool-call count have limits; hitting one ends the run with the limit named. |

## E. Projects

| # | Requirement | Met when |
|---|---|---|
| OR-E1 | Register a project: a name, a root directory, a default provider and model, and a capability profile. | A run started in a project inherits those defaults, and the run header shows them. |
| OR-E2 | Refuse a project root that overlaps a protected location. | The registration fails with the rule that blocked it named. |
| OR-E3 | Search and read files under a project root. | Reading stays inside the root, and a symbolic link out of it is refused rather than followed. |
| OR-E4 | Open a project in the user's editor, a terminal or a file browser. | The launch is a user action, and the operator does not run project commands on its own. |
| OR-E5 | Show a project's source-control state. | The current branch, whether the tree is dirty, and unpushed work are visible without leaving the operator. |

## F. Specs and plans

| # | Requirement | Met when |
|---|---|---|
| OR-F1 | Track specification documents per project. | Each tracked document shows its path, its current content hash and whether it is approved. |
| OR-F2 | Approve a document by its content. | The approval is bound to the hash it was given for; any edit withdraws it, and the operator shows the document as unapproved again. |
| OR-F3 | Review a plan before it runs. | The user sees the plan, and the run starts only after it is accepted; rejecting it ends the run rather than continuing on the old plan. |
| OR-F4 | Show what changed between the approved version and the current one. | The difference is legible without an external tool. |
| OR-F5 | Require an approved spec where a project demands one. | A run in such a project cannot start against an unapproved document, and says which document blocked it. |

## G. Approvals and capability profiles

| # | Requirement | Met when |
|---|---|---|
| OR-G1 | Offer named capability profiles covering file reads, file writes, shell use, network use and side effects. | Every run records the profile it ran under, and the profile is visible while it runs. |
| OR-G2 | Ask before a side effect. | Publishing, installing, deploying, deleting and sending require a decision, and the request names the exact action. |
| OR-G3 | Time out an unanswered approval. | The run ends rather than proceeding, and the timeout is recorded as such. |
| OR-G4 | Scope a granted permission. | A grant applies to the run it was given for and expires with it, unless the user widens it deliberately. |
| OR-G5 | Withdraw permission from a run that has read untrusted content. | After reading web, mail, repository or tool output from outside the workspace, the run needs a fresh approval for any side effect. |
| OR-G6 | Refuse writes to protected paths outright. | No profile, provider argument or shell invocation can reach them, and the attempt is recorded. |
| OR-G7 | Record every approval decision. | The record holds who decided, when, what was asked and what was decided. |

## H. Usage

| # | Requirement | Met when |
|---|---|---|
| OR-H1 | Show consumption per provider. | Rate limits and remaining budget appear where the provider reports them. |
| OR-H2 | Say "not available" where a provider reports nothing. | The view never shows a zero, an estimate or a stale number in place of an unavailable one. |
| OR-H3 | Show providers that share one subscription as one budget. | Two providers drawing on the same account are not presented as two independent allowances. |
| OR-H4 | Warn before the limit, not after. | A configurable threshold raises a warning that names the provider and what will stop working. |
| OR-H5 | Keep usage history. | Consumption over time is readable, with gaps shown as gaps. |

## I. Automation

| # | Requirement | Met when |
|---|---|---|
| OR-I1 | Create, edit, pause, resume, delete and run scheduled jobs. | Every operation is available without leaving the operator, and the schedule is expressed in a named time zone. |
| OR-I2 | Pin provider and model at creation time. | A later global change leaves existing jobs untouched. |
| OR-I3 | Define what happens to a missed run. | The choices are skip, run once, or catch up to a cap, and the choice made is visible on the job. |
| OR-I4 | Define what happens when a run overlaps its predecessor. | The choices are skip, queue, or replace, and the outcome is recorded per occurrence. |
| OR-I5 | Forbid a job from creating jobs. | A run that tries is refused and the attempt is recorded; only the user can lift it, per job. |
| OR-I6 | Deliver a job's result. | The user chooses the channel; a delivery that fails is reported rather than dropped. |
| OR-I7 | Stop everything at once. | A single control halts scheduled work and refuses new dispatch until it is released. |
| OR-I8 | Show a job's history. | Past occurrences, their status, their duration and their output are readable per job. |

## J. Memory

| # | Requirement | Met when |
|---|---|---|
| OR-J1 | Search stored memory and show where each entry came from. | Each result names its origin: the user said it, an agent inferred it, or it came from outside. |
| OR-J2 | Keep untrusted content out of instructions. | Externally sourced text is presented as data, and is never lifted into a system prompt. |
| OR-J3 | Delete a memory entry. | Deletion removes it from every index it appears in, and is recorded. |
| OR-J4 | Export everything. | The export is complete, machine-readable and carries provenance. |
| OR-J5 | Expire memory by class. | Entries carry a retention class, and expiry runs without a user action. |

## K. Audit

| # | Requirement | Met when |
|---|---|---|
| OR-K1 | Record security-relevant events. | Model changes, profile changes, approval decisions, blocked path access, deletions, job changes, logins and fallbacks are all present. |
| OR-K2 | Make the record tamper-evident. | An entry cannot be edited or removed through the application, and a break in the chain is detectable. |
| OR-K3 | Make the record readable. | The user can filter by time, run and kind of event without an external tool. |
| OR-K4 | Keep secrets out of it. | No credential, token or key appears in an event, and this holds for events recording an attempt to read one. |

## L. Notifications

| # | Requirement | Met when |
|---|---|---|
| OR-L1 | Notify when a run needs the user. | The notification names the run and leads to the place where it can be answered. |
| OR-L2 | Notify when unattended work finishes or fails. | Scheduled and background runs report both outcomes. |
| OR-L3 | Rank spoken output. | Errors and questions are spoken before results, and results before background chatter. |
| OR-L4 | Repeat a spoken answer without re-running it. | Repeating replays the stored answer and starts no new work. |

## Non-goals

- Access from another machine, a hosted service, or a mobile client.
- Editing files or running commands directly in the operator. It starts agents and the user's own
  tools; it is not an editor and not a shell.
- Managing provider subscriptions, billing or accounts beyond triggering the provider's own login.
- Bundling or updating the provider CLIs. They are separate products with their own release cycle;
  the operator detects them and reports what it finds.
- Any capability that depends on a cloud service the user cannot replace with their own endpoint.

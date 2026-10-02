---
name: keep-modules-simple
description: Use when creating or substantially changing code to decide when to extract functions, files, or packages and keep responsibilities clear as the code grows. Skip formatting-only and trivial edits.
metadata:
  short-description: Choose clear code boundaries
---

# Keep modules simple

Make code easy to understand and change. Here, a *module* means a cohesive unit of code; depending on the language, that may be a function, class, file, package, or library. Follow the project's existing structure and the scope of the requested change.

## Choose the smallest useful boundary

First name the responsibility of the code you are changing. A good boundary groups behavior that changes for the same reason and exposes a small, understandable interface. Extract only when the new boundary makes the caller or the extracted code clearer.

- **Function:** Extract a distinct, nameable step; nontrivial repeated logic; or details that obscure the caller's main flow. Keep a short, cohesive sequence inline when a helper would merely forward arguments or force many values through its signature. Around 50 lines is a prompt to inspect a function, not a mandatory limit.
- **Class or type:** Introduce one when state, invariants, and operations belong together. Keep stateless operations as functions when a class would only serve as a namespace.
- **File:** Move a cohesive set of functions or types when it has a separate responsibility, dependency, or change pattern, and the resulting interface is clear. Keep closely related code together when readers normally need to see it at once. Do not split files solely because they are long.
- **Package or larger module:** Group related files when they form a distinct capability with a useful public API or a meaningful dependency boundary. Avoid a new package for a single helper or to mirror a directory hierarchy without benefit.

Prefer code near its users over a generic `utils` module unless the same code is used across the project. Do not extract speculative abstractions for one use case. If several callers need the same behavior, identify the shared concept before moving it.

## Keep abstraction levels and dependencies clear

Let a function describe one level of work. When low-level parsing, I/O, or third-party API details interrupt the main algorithm, give those details a named boundary. An adapter should translate between interfaces or contain real integration logic; a pass-through wrapper adds little value.

For more complex features, consider three roles from the draft: the outward API, the core implementation, and adapters to other modules or external systems. These are responsibilities, not a required number of files or layers. Keep dependency direction understandable and avoid cycles between modules.

## Apply and verify

Inspect nearby code, callers, and tests before changing boundaries. Make the smallest extraction that clarifies the responsibility; use names that describe behavior rather than implementation mechanics. Update imports and callers, then run the relevant tests or checks. Preserve observable behavior and public APIs unless the task explicitly calls for a change.

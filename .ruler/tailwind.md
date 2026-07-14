---
description: Tailwind CSS size utility preference (web frontend only)
globs: ["apps/web/**/*.tsx", "apps/web/**/*.jsx"]
alwaysApply: false
---

# Tailwind CSS Size Utility Guidelines

**Scope:** This rule applies only to `apps/web/` (Next.js frontend). Backend and mobile apps do not use Tailwind CSS.

## Rule: Use `size-*` for Square Dimensions

**Guideline 1:** Use Tailwind's `size-*` utility classes instead of separate `w-*` and `h-*` when width and height are the same.

**Examples:**

```tsx
// ✅ GOOD - Using size-* for equal dimensions
<div className="size-4" />
<div className="size-6" />
<div className="size-8" />
<div className="size-12" />

// ❌ BAD - Redundant w-* and h-* when equal
<div className="w-4 h-4" />
<div className="w-6 h-6" />
```

**Guideline 2:** Only use separate `w-*` and `h-*` classes when the width and height values are different.

```tsx
// ✅ GOOD - Different dimensions require separate utilities
<div className="w-full h-screen" />
<div className="w-64 h-48" />
```

**Guideline 3:** This applies to all size values including responsive variants.

```tsx
// ✅ GOOD - Responsive size utilities
<div className="size-4 md:size-6 lg:size-8" />

// ❌ BAD - Redundant responsive utilities
<div className="w-4 h-4 md:w-6 md:h-6 lg:w-8 lg:h-8" />
```

## Common Use Cases

| Element          | Preferred                      | Avoid                  |
| ---------------- | ------------------------------ | ---------------------- |
| Icons            | `size-4`, `size-5`, `size-6`   | `w-4 h-4`, `w-5 h-5`   |
| Avatars          | `size-8`, `size-10`, `size-12` | `w-8 h-8`, `w-10 h-10` |
| Buttons (square) | `size-9`, `size-10`            | `w-9 h-9`, `w-10 h-10` |
| Loaders          | `size-4`, `size-6`             | `w-4 h-4`, `w-6 h-6`   |

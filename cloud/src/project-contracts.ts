import { z } from "zod";
export const MAX_BYTES = 524288;
export const projectPath = z
  .string()
  .min(1)
  .max(500)
  .refine(
    (path) =>
      !/[\\\x00-\x1f\x7f]/.test(path) &&
      path
        .split("/")
        .every(
          (part) =>
            part.length > 0 && !part.startsWith(".") && !part.includes(":"),
        ),
    "Use a relative document path without hidden components or traversal.",
  );
export const revision = z.string().regex(/^[a-f0-9]{64}$/);
export const changeSchema = z.object({
  path: projectPath.refine(
    (path) => path.toLowerCase().endsWith(".md"),
    "Only Markdown documents can be saved.",
  ),
  text: z.string().min(1).max(MAX_BYTES),
  expected_revision: revision.nullable(),
  request_id: z.string().regex(/^[a-zA-Z0-9][a-zA-Z0-9._-]{7,127}$/),
});

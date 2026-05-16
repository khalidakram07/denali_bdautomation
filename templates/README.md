# Email Templates

Drop `.docx` files here. Each becomes a selectable option in the "Template"
dropdown when generating an email draft.

## Template syntax

Two kinds of tokens:

### `{placeholders}` — direct substitution
Replaced verbatim with data from the contact + opportunity:

- `{first_name}`, `{last_name}`, `{full_name}`, `{title}`
- `{company}` (= cro_name OR sponsor_name)
- `{sponsor_name}`, `{cro_name}`
- `{trial_title}`, `{phase}`, `{indication}`, `{therapeutic_area}`
- `{geography}`, `{sites_needed}`
- `{drug_names}` (semicolon-separated from Clinwire Drugs field)
- `{sender_name}` (from DEFAULT_SENDER_NAME env var)
- `{source_url}` (from Clinwire Source URL)

Unknown placeholders are left as-is — you'll see them in the draft, can edit/remove.

### `[BRACKETED INSTRUCTIONS]` — AI fills in
Capital-letter instructions in brackets are replaced by AI-generated text.
The AI sees the Clinwire **Full Text** article and uses it as primary context.

Example:
> [OPEN: Reference one specific milestone from the source article. One sentence.]

## Special first line: SUBJECT:
If the template's first non-empty line starts with `SUBJECT:`, everything after
that prefix becomes the email subject. Body starts on the next non-empty line.

## Tips
- Word's auto-correct often replaces `{` with smart braces. If placeholders stop working, check that Word didn't autocorrect them. (Edit > Preferences > AutoCorrect > Replace Text As You Type)
- File names appear in the dropdown — `first_touch_default.docx` shows as "First Touch Default". Keep names short.
- Word lock files (`~$something.docx`) are auto-ignored.

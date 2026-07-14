# Email Signature Logos

Drop signature logo images here. They're served as public URLs and can be
referenced from the `signature_html` field in `MAILBOXES_JSON`.

**Public URL format:** `https://denali-bd.onrender.com/static/signatures/<filename>`

**Recommended:**
- PNG or JPG (transparent PNG for logos over dark inboxes)
- Max height ~60px, max width ~200px
- Keep file size under 50KB (email clients throttle large images)
- Use meaningful filenames: `denali-logo.png`, `doaa-signature.png`

**Example `signature_html`:**

```html
<div style="font-family:Arial,sans-serif;font-size:13px;color:#333;">
  <div style="font-weight:bold;">Doaa Abasher</div>
  <div>Business Development, Denali Health</div>
  <div><a href="mailto:doaa@denali-health.com">doaa@denali-health.com</a></div>
  <div style="margin-top:8px;">
    <img src="https://denali-bd.onrender.com/static/signatures/denali-logo.png"
         alt="Denali Health" style="max-height:60px;">
  </div>
</div>
```

**How to get your current Gmail signature HTML:**

1. Open Gmail → gear icon → See all settings → General tab
2. Scroll to Signature section — copy the visible signature text/formatting
3. For the exact HTML, use browser DevTools: right-click the signature preview → Inspect
   → copy the `<div>` element that contains it
4. Paste into `signature_html` in `MAILBOXES_JSON` (escape quotes as `\"`)
5. Upload any logo images to this folder, commit to git, redeploy

**Alternative** (no code changes): send yourself a test email from Gmail,
then view source (View → Show Original) — the signature HTML is in the body.

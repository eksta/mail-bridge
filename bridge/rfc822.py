"""RFC822 <-> API JSON conversion for both providers."""
import email
import email.policy
import email.utils
import html as html_mod
import re
from email.message import EmailMessage


def _decode_hdr(v):
    if not v:
        return ""
    return str(email.header.make_header(email.header.decode_header(v)))


def addr_list_to_header(items):
    """API correspondents [{name,email}] or ["a@b"] -> header value."""
    out = []
    for it in items or []:
        if isinstance(it, dict):
            name = it.get("name") or ""
            mail = it.get("email") or it.get("address") or ""
            out.append(email.utils.formataddr((name, mail)) if mail else name)
        else:
            out.append(str(it))
    return ", ".join(out)


# ---------------------------------------------------------------- yandex
def yandex_envelope_to_message(meta, body_json, default_email):
    """MessageMetaJson + MessageBodyJson -> EmailMessage."""
    m = EmailMessage()
    subj = (meta.get("subjText") or "")
    if meta.get("subjPrefix"):
        subj = f"Re: {subj}"
    m["Subject"] = subj or "(no subject)"
    m["From"] = addr_list_to_header(_as_list(meta.get("from"))
                                    or [{"email": default_email}])
    m["To"] = addr_list_to_header(_as_list(meta.get("recipients")))
    ts = meta.get("utc_timestamp") or meta.get("timestamp") or 0
    try:
        ts = int(str(ts)) if str(ts).isdigit() else 0
    except (ValueError, TypeError):
        ts = 0
    if ts > 10**12:  # milliseconds
        ts //= 1000
    m["Date"] = email.utils.formatdate(ts, usegmt=True) if ts \
        else email.utils.formatdate()
    m["X-Bridge-Mid"] = str(meta.get("mid", ""))
    m["Message-ID"] = f"<{meta.get('mid', 'unknown')}@bridge.yandex>"

    body_obj = {}
    html = ""
    if isinstance(body_json, dict):
        b = body_json.get("body", body_json)
        if isinstance(b, list):
            # message_body real format: body=[{hid, content}, ...]
            html = "".join(p.get("content", "") for p in b
                           if isinstance(p, dict))
        elif isinstance(b, dict):
            html = b.get("body") or b.get("content") or ""
        else:
            html = str(b or "")
    text = _html_to_text(html)
    if html:
        m.set_content(text or " ")
        m.add_alternative(html, subtype="html")
    else:
        m.set_content(text)

    # attachments from info.attachments (placeholders; bytes fetched lazily)
    atts = []
    if isinstance(body_json, dict):
        info = body_json.get("info", {})
        atts = info.get("attachments") or []
    m._bridge_atts = []
    for a in atts:
        if a.get("is_inline"):
            continue  # inline images: referenced by cid, keep html only
        maintype, _, subtype = (a.get("mime_type") or "application/"
                                "octet-stream").partition("/")
        try:
            m.add_attachment(b"", maintype=maintype or "application",
                             subtype=subtype or "octet-stream",
                             filename=a.get("display_name") or "attachment")
        except Exception:
            continue
        part = list(m.iter_parts())[-1] if m.is_multipart() else None
        if part is not None:
            part["X-Bridge-Hid"] = str(a.get("hid", ""))
            part["X-Bridge-Size"] = str(a.get("size", 0))
            m._bridge_atts.append(str(a.get("hid", "")))
    return m


def _as_list(v):
    """API returns from: {..} or [ {..} ] — normalize to list."""
    if v is None:
        return []
    if isinstance(v, dict):
        return [v]
    return v


# ---------------------------------------------------------------- outgoing
def parse_outgoing(raw_bytes):
    """Raw message from SMTP DATA -> dict(to,cc,bcc,subject,html,text,in_reply_to,attachments)."""
    msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
    to = msg.get("To", "")
    cc = msg.get("Cc", "")
    bcc = msg.get("Bcc", "")
    subject = _decode_hdr(msg.get("Subject", ""))
    html = None
    text = None
    attachments = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get_content_disposition() or "")
            if disp == "attachment":
                # Extract attachment data
                payload = part.get_payload(decode=True)
                if payload is not None:
                    attachments.append({
                        "filename": part.get_filename() or "attachment",
                        "content_type": ctype,
                        "data": payload,
                    })
                continue
            if ctype == "text/plain" and text is None:
                text = part.get_content()
            elif ctype == "text/html" and html is None:
                html = part.get_content()
    else:
        ctype = msg.get_content_type()
        try:
            content = msg.get_content()
        except Exception:
            content = msg.get_payload(decode=True)
            content = content.decode("utf-8", "replace") if isinstance(content, bytes) else str(content)
        if ctype == "text/html":
            html = content
        else:
            text = content
    return {
        "to": _split_addr(to),
        "cc": _split_addr(cc),
        "bcc": _split_addr(bcc),
        "subject": subject,
        "html": html,
        "text": text if text is not None else (_html_to_text(html) if html else ""),
        "in_reply_to": str(msg.get("In-Reply-To", "") or ""),
        "references": str(msg.get("References", "") or ""),
        "message_id": str(msg.get("Message-ID", "") or ""),
        "to_header": str(to or ""),
        "cc_header": str(cc or ""),
        "date_header": str(msg.get("Date", "") or ""),
        "attachments": attachments,
    }


def _split_addr(header_val):
    return [a for _, a in email.utils.getaddresses([header_val or ""]) if a]


def _html_to_text(html):
    txt = re.sub(r"(?i)<br\s*/?>", "\n", html or "")
    txt = re.sub(r"(?i)</p>", "\n", txt)
    txt = re.sub(r"(?i)</div>", "\n", txt)
    txt = re.sub(r"<[^>]+>", "", txt)
    return html_mod.unescape(txt).strip()

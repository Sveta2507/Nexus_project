"""Small dependency-free Chromium DevTools check of the real browser UI.

Start a Nexus node and Chromium/Edge with remote debugging, then run:
python tests/browser_check.py http://127.0.0.1:9223 [screenshot.png] [URL-marker]
"""

import json
import sys
import time
import urllib.request
import base64
from pathlib import Path

from websockets.sync.client import connect


class DevTools:
    def __init__(self, websocket_url: str):
        self.socket = connect(websocket_url, origin="http://localhost", legacy=True)
        self.next_id = 1
        self.exceptions: list[dict] = []

    def close(self) -> None:
        self.socket.close()

    def call(self, method: str, params: dict | None = None) -> dict:
        request_id = self.next_id
        self.next_id += 1
        self.socket.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.socket.recv(timeout=10))
            if message.get("method") == "Runtime.exceptionThrown":
                self.exceptions.append(message["params"])
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(message["error"])
                return message.get("result", {})

    def evaluate(self, expression: str):
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        return result.get("result", {}).get("value")


def main() -> None:
    endpoint = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:9223").rstrip("/")
    url_marker = sys.argv[3] if len(sys.argv) > 3 else ""
    deadline = time.time() + 10
    targets = []
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{endpoint}/json", timeout=1) as response:
                targets = json.load(response)
            if targets:
                break
        except OSError:
            time.sleep(0.1)
    target = next(
        (
            item for item in targets
            if item.get("type") == "page"
            and item.get("url", "").startswith(("http://", "https://"))
            and (not url_marker or url_marker in item.get("url", ""))
        ),
        None,
    )
    if not target:
        raise RuntimeError("Nexus browser target was not found")

    tools = DevTools(target["webSocketDebuggerUrl"])
    try:
        tools.call("Runtime.enable")
        tools.call("Page.enable")
        tools.evaluate("localStorage.clear()")
        tools.call("Page.reload")
        time.sleep(0.25)
        deadline = time.time() + 10
        while time.time() < deadline and tools.evaluate("document.readyState") != "complete":
            time.sleep(0.1)

        username = f"browser_{int(time.time())}"
        tools.evaluate("document.querySelector('#auth-switch').click()")
        tools.evaluate("document.querySelector('#display-name').value='Browser Test'")
        tools.evaluate(f"document.querySelector('#username').value={json.dumps(username)}")
        tools.evaluate("document.querySelector('#password').value='Browser-password-123'")
        tools.evaluate("document.querySelector('#auth-form').requestSubmit()")

        deadline = time.time() + 12
        while time.time() < deadline:
            if tools.evaluate("!document.querySelector('#app').classList.contains('hidden')"):
                break
            time.sleep(0.15)
        else:
            error = tools.evaluate("document.querySelector('#auth-error').textContent")
            raise AssertionError(f"Browser registration did not open the application: {error}")

        assert tools.evaluate("document.querySelector('#profile-name').textContent") == "Browser Test"
        deadline = time.time() + 10
        while time.time() < deadline and not tools.evaluate("document.querySelector('#channel-list .channel') !== null"):
            time.sleep(0.1)
        assert tools.evaluate("document.querySelector('#connection-dot').classList.contains('offline')") is False
        if tools.evaluate("document.querySelector('#toggle-people').getAttribute('aria-expanded')") != "true":
            tools.evaluate("document.querySelector('#toggle-people').click()")
        assert tools.evaluate("document.querySelector('#toggle-people').getAttribute('aria-expanded')") == "true"
        assert tools.evaluate("state.peoplePanelOpen") is True
        tools.evaluate("document.querySelector('#close-people').click()")
        assert tools.evaluate("document.querySelector('#toggle-people').getAttribute('aria-expanded')") == "false"
        tools.evaluate("document.querySelector('#open-shortcuts').click()")
        assert tools.evaluate("!document.querySelector('#shortcuts-modal').classList.contains('hidden')") is True
        assert tools.evaluate("document.querySelectorAll('#shortcuts-modal kbd').length >= 9") is True
        tools.evaluate("document.querySelector('#close-shortcuts').click()")
        assert tools.evaluate(
            "document.querySelector('#audio-toggle > svg.mic-icon') !== null"
            " && document.querySelector('#audio-toggle .mic-icon rect') !== null"
        ) is True
        assert tools.evaluate(
            "(()=>{const style=getComputedStyle(document.querySelector('#message-input'));"
            "return style.paddingTop===style.paddingBottom&&parseFloat(style.minHeight)>=39})()"
        ) is True
        assert tools.evaluate(
            "parseFloat(getComputedStyle(document.querySelector('#message-list')).paddingBottom)<=10"
        ) is True

        tools.evaluate("document.querySelector('#create-channel-button').click()")
        assert tools.evaluate(
            "(()=>{const form=document.querySelector('#channel-form');"
            "return form.scrollWidth<=form.clientWidth&&!!form.querySelector('.toggle-switch')"
            "&&!!form.querySelector('.channel-members-section')})()"
        ) is True
        assert tools.evaluate(
            "(()=>{const section=getComputedStyle(document.querySelector('.channel-members-section'));"
            "const list=getComputedStyle(document.querySelector('.channel-member-list'));"
            "return parseFloat(section.borderTopWidth)===0&&parseFloat(list.borderTopWidth)===1})()"
        ) is True
        tools.evaluate("document.querySelector('#new-channel-name').value='browser-e2ee'")
        tools.evaluate("document.querySelector('#new-channel-description').value='Browser cryptography check'")
        tools.evaluate("document.querySelector('#channel-form').requestSubmit()")
        deadline = time.time() + 12
        while time.time() < deadline:
            if (
                tools.evaluate("document.querySelector('#channel-modal').classList.contains('hidden')")
                and tools.evaluate("document.querySelector('#channel-name').textContent") == "browser-e2ee"
            ):
                break
            time.sleep(0.15)
        else:
            error = tools.evaluate("document.querySelector('#channel-error').textContent")
            raise AssertionError(f"Encrypted channel was not created: {error}")

        secret_message = "browser plaintext must stay client-side"
        tools.evaluate(f"document.querySelector('#message-input').value={json.dumps(secret_message)}")
        tools.evaluate("document.querySelector('#message-form').requestSubmit()")
        deadline = time.time() + 10
        while time.time() < deadline:
            if secret_message in (tools.evaluate("document.querySelector('#message-list').innerText") or ""):
                break
            time.sleep(0.1)
        else:
            raise AssertionError("Encrypted message wasn't decrypted in the browser")
        deadline = time.time() + 10
        stored_messages = []
        while time.time() < deadline and not stored_messages:
            stored_messages = tools.evaluate(
                "(async()=>await api(`/api/messages?room_id=${encodeURIComponent(state.activeChannel.id)}`))()"
            )["messages"]
            if not stored_messages:
                time.sleep(0.1)
        assert stored_messages, "Encrypted message was not persisted by the server"
        encrypted_message = stored_messages[-1]
        assert encrypted_message["body"] == ""
        assert encrypted_message["encrypted"] == 1 and encrypted_message["ciphertext"]
        assert secret_message not in json.dumps(encrypted_message)

        tools.evaluate("document.querySelector('#open-documents').click()")
        deadline = time.time() + 5
        while time.time() < deadline and not tools.evaluate(
            "document.querySelector('#documents-drawer').classList.contains('open')"
        ):
            time.sleep(0.1)
        assert tools.evaluate("document.querySelector('#documents-drawer').classList.contains('open')") is True
        assert tools.evaluate(
            "document.querySelector('#document-kind')===null"
            "&&document.querySelector('#page-count')===null"
            "&&document.querySelector('.upload-hint').textContent.includes('автоматично')"
        ) is True
        assert tools.evaluate(
            "document.querySelector('#zoom-out')!==null"
            "&&document.querySelector('#zoom-reset').textContent==='100%'"
            "&&document.querySelector('#zoom-in')!==null"
        ) is True
        tools.evaluate("document.querySelector('[data-close=\"documents\"]').click()")
        zoom_geometry = tools.evaluate(
            "(async()=>{"
            "const canvas=document.createElement('canvas');canvas.width=800;canvas.height=600;"
            "const context=canvas.getContext('2d');context.fillStyle='#3557d4';context.fillRect(0,0,800,600);"
            "const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/png'));"
            "const data=new FormData();data.append('file',new File([blob],'zoom-check.png',{type:'image/png'}));"
            "data.append('channel_id',state.activeChannel.id);"
            "const result=await api('/api/documents',{method:'POST',body:data});"
            "await openDocument(result.document);const image=document.querySelector('#document-viewer img');"
            "if(!image.complete)await new Promise((resolve,reject)=>{image.onload=resolve;image.onerror=reject});"
            "await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));"
            "const viewer=document.querySelector('#document-viewer');const at100=image.getBoundingClientRect();"
            "setDocumentZoom(2);await new Promise(resolve=>requestAnimationFrame(resolve));"
            "const at200=image.getBoundingClientRect();const overflowAt200=viewer.scrollWidth>viewer.clientWidth;"
            "state.presenting=true;setStageMode('presenter');updatePresentationSurfaceInsets();"
            "const surface=document.querySelector('#presentation-surface');"
            "const surfaceBottomGap=viewer.getBoundingClientRect().bottom-surface.getBoundingClientRect().bottom;"
            "viewer.scrollLeft=0;surface.dispatchEvent(new WheelEvent('wheel',"
            "{deltaY:180,shiftKey:true,deltaMode:0,cancelable:true,bubbles:true}));"
            "const horizontalWheelWorked=viewer.scrollLeft>0;state.presenting=false;setStageMode('local');"
            "setDocumentZoom(.5);await new Promise(resolve=>requestAnimationFrame(resolve));"
            "const at50=image.getBoundingClientRect();const viewport=viewer.getBoundingClientRect();"
            "return {ratio200:at200.width/at100.width,ratio50:at50.width/at100.width,overflowAt200,"
            "surfaceBottomGap,horizontalWheelWorked,"
            "centeredAt50:Math.abs((at50.left+at50.width/2)-(viewport.left+viewport.width/2))<2}})()"
        )
        assert abs(zoom_geometry["ratio200"] - 2) < 0.03, zoom_geometry
        assert abs(zoom_geometry["ratio50"] - 0.5) < 0.03, zoom_geometry
        assert zoom_geometry["overflowAt200"] is True, zoom_geometry
        assert zoom_geometry["surfaceBottomGap"] >= 15, zoom_geometry
        assert zoom_geometry["horizontalWheelWorked"] is True, zoom_geometry
        assert zoom_geometry["centeredAt50"] is True, zoom_geometry
        tools.evaluate("closeStage()")
        other_format_zoom = tools.evaluate(
            "(async()=>{"
            "async function uploadAndOpen(name,type,body){const data=new FormData();"
            "data.append('file',new File([body],name,{type}));data.append('channel_id',state.activeChannel.id);"
            "const result=await api('/api/documents',{method:'POST',body:data});await openDocument(result.document);"
            "await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));}"
            "await uploadAndOpen('zoom-check.txt','text/plain','Nexus zoom line\\n'.repeat(80));"
            "const text=document.querySelector('.text-document');const text100=text.getBoundingClientRect().width;"
            "setDocumentZoom(2);await new Promise(resolve=>requestAnimationFrame(resolve));"
            "const text200=text.getBoundingClientRect().width;closeStage();"
            "await uploadAndOpen('zoom-check.pdf','application/pdf',"
            "'%PDF-1.4\\n1 0 obj << /Type /Page >> endobj\\n');"
            "const frame=document.querySelector('#document-viewer iframe');const pdf100=frame.getBoundingClientRect().width;"
            "setDocumentZoom(.5);await new Promise(resolve=>requestAnimationFrame(resolve));"
            "const pdf50=frame.getBoundingClientRect().width;closeStage();"
            "return {text200:text200/text100,pdf50:pdf50/pdf100}})()"
        )
        assert abs(other_format_zoom["text200"] - 2) < 0.03, other_format_zoom
        assert abs(other_format_zoom["pdf50"] - 0.5) < 0.03, other_format_zoom
        tools.evaluate("document.querySelector('#open-system').click()")
        deadline = time.time() + 5
        while time.time() < deadline and tools.evaluate("document.querySelector('#system-modal').classList.contains('hidden')"):
            time.sleep(0.1)
        system_text = tools.evaluate("document.querySelector('#system-grid').innerText")
        active_node_id = tools.evaluate("state.nodeId")
        assert active_node_id in system_text and "WEBSOCKET" in system_text, (
            f"System modal did not render: {system_text!r}"
        )
        assert tools.evaluate("document.querySelectorAll('#system-grid .system-info-button').length") == 13
        assert tools.evaluate(
            "(()=>{const row=document.querySelector('#system-grid .system-item-value');"
            "const value=row.querySelector('strong').getBoundingClientRect();"
            "const info=row.querySelector('button').getBoundingClientRect();"
            "return Math.abs((value.top+value.height/2)-(info.top+info.height/2))<2})()"
        ) is True
        assert tools.evaluate(
            "(()=>{const card=document.querySelector('#system-modal .system-card');"
            "card.scrollTop=card.scrollHeight;const link=card.querySelector('.panel-help').getBoundingClientRect();"
            "return link.bottom<=window.innerHeight+1&&link.top>=-1})()"
        ) is True
        tools.evaluate("document.querySelector('#system-grid .system-info-button').click()")
        assert tools.evaluate("!document.querySelector('#diagnostic-help-modal').classList.contains('hidden')") is True
        assert tools.evaluate("document.querySelector('#diagnostic-help-summary').textContent.length > 20") is True
        assert tools.evaluate("document.querySelector('#diagnostic-help-purpose').textContent.length > 20") is True
        tools.evaluate("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))")
        assert tools.evaluate("document.querySelector('#diagnostic-help-modal').classList.contains('hidden')") is True
        if len(sys.argv) > 2:
            tools.evaluate("closeSystem()")
            screenshot = tools.call("Page.captureScreenshot", {"format": "png"})["data"]
            Path(sys.argv[2]).write_bytes(base64.b64decode(screenshot))
        assert not tools.exceptions, f"Browser JavaScript exceptions: {tools.exceptions}"
        print("OK: browser registration, E2EE, WebSocket, image/TXT/PDF zoom 50-200% and system modal")
    finally:
        tools.close()


if __name__ == "__main__":
    main()

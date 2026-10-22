"""Verify a real E2EE message between two browsers connected to different nodes."""

import json
import sys
import time
import urllib.request

from browser_check import DevTools


PASSWORD = "Browser-password-123"


def connect_page(endpoint: str, url_marker: str) -> DevTools:
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{endpoint.rstrip('/')}/json", timeout=1) as response:
                targets = json.load(response)
            target = next(
                (item for item in targets if item.get("type") == "page" and url_marker in item.get("url", "")),
                None,
            )
            if target:
                return DevTools(target["webSocketDebuggerUrl"])
        except OSError:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"Browser target wasn't found at {endpoint}")


def wait_value(tools: DevTools, expression: str, timeout: float = 15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = tools.evaluate(expression)
        if value:
            return value
        time.sleep(0.15)
    raise AssertionError(f"Browser condition timed out: {expression}")


def register(tools: DevTools, username: str, display_name: str) -> None:
    tools.call("Runtime.enable")
    tools.call("Page.enable")
    tools.evaluate("localStorage.clear()")
    tools.call("Page.reload")
    time.sleep(0.25)
    wait_value(tools, "document.readyState === 'complete'")
    tools.evaluate("document.querySelector('#auth-switch').click()")
    tools.evaluate(f"document.querySelector('#display-name').value={json.dumps(display_name)}")
    tools.evaluate(f"document.querySelector('#username').value={json.dumps(username)}")
    tools.evaluate(f"document.querySelector('#password').value={json.dumps(PASSWORD)}")
    tools.evaluate("document.querySelector('#auth-form').requestSubmit()")
    wait_value(tools, "!document.querySelector('#app').classList.contains('hidden')")
    wait_value(tools, "document.querySelector('#channel-list .channel') !== null")
    wait_value(tools, "state.device !== null")


def main() -> None:
    if len(sys.argv) != 5:
        raise SystemExit("Usage: e2ee_two_browser_check.py EDGE_A EDGE_B NODE_PORT_A NODE_PORT_B")
    edge_a, edge_b, node_a, node_b = sys.argv[1:]
    alice = connect_page(edge_a, node_a)
    bob = connect_page(edge_b, node_b)
    try:
        register(alice, "crypto_alice", "Crypto Alice")
        register(bob, "crypto_bob", "Crypto Bob")
        alice.call("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 800, "deviceScaleFactor": 1, "mobile": False})
        bob.call("Emulation.setDeviceMetricsOverride", {"width": 760, "height": 640, "deviceScaleFactor": 1, "mobile": False})
        assert alice.evaluate("window.innerWidth") != bob.evaluate("window.innerWidth")

        wait_value(
            alice,
            "state.users.some(user => user.username === 'crypto_bob')",
            timeout=20,
        )
        wait_value(
            alice,
            "(async()=>{const r=await api('/api/keys/devices?usernames=crypto_bob');"
            "return r.devices.some(d=>d.username==='crypto_bob')})()",
            timeout=20,
        )

        alice.evaluate("document.querySelector('#create-channel-button').click()")
        alice.evaluate("document.querySelector('#new-channel-name').value='cross-node-e2ee'")
        alice.evaluate("document.querySelector('#new-channel-description').value='Two browser E2EE test'")
        alice.evaluate(
            "[...document.querySelectorAll('#channel-member-list input')]"
            ".find(item=>item.value==='crypto_bob').checked=true"
        )
        alice.evaluate("document.querySelector('#channel-form').requestSubmit()")
        wait_value(alice, "document.querySelector('#channel-name').textContent === 'cross-node-e2ee'", 20)
        wait_value(
            bob,
            "[...document.querySelectorAll('#channel-list .channel')]"
            ".some(item=>item.textContent.includes('cross-node-e2ee'))",
            20,
        )
        bob.evaluate(
            "[...document.querySelectorAll('#channel-list .channel')]"
            ".find(item=>item.textContent.includes('cross-node-e2ee')).click()"
        )
        wait_value(bob, "document.querySelector('#channel-name').textContent === 'cross-node-e2ee'")

        secret = "cross-node secret visible only in browsers"
        alice.evaluate(f"document.querySelector('#message-input').value={json.dumps(secret)}")
        alice.evaluate("document.querySelector('#message-form').requestSubmit()")
        wait_value(
            bob,
            f"document.querySelector('#message-list').innerText.includes({json.dumps(secret)})",
            20,
        )
        wait_value(
            alice,
            "document.querySelector('#message-list .message-delivery.read')?.textContent.includes('Прочитано')",
            20,
        )
        assert bob.evaluate(
            "document.querySelector('#message-list .message-date-separator')?.textContent.trim()"
        ) == "Сьогодні"
        assert bob.evaluate(
            "document.querySelector('#message-list .message time')?.dateTime.includes('T')"
        ) is True
        assert bob.evaluate(
            "document.querySelector('#message-list .message time')?.getAttribute('aria-label').length>10"
        ) is True
        assert bob.evaluate("formatCalendarDate(Date.now()/1000-86400)") == "Вчора"
        assert bob.evaluate("viewerCountLabel(1)") == "1 глядач"
        assert bob.evaluate("viewerCountLabel(2)") == "2 глядачі"
        assert bob.evaluate("viewerCountLabel(5)") == "5 глядачів"
        assert bob.evaluate(
            "document.querySelector('#message-form > #audio-toggle') !== null"
            " && document.querySelector('.top-actions > #audio-toggle') === null"
        ) is True
        assert bob.evaluate(
            "(async()=>{await loadMessages();return document.querySelectorAll('#message-list .message-date-separator').length})()"
        ) == 1
        stored = bob.evaluate(
            "(async()=>await api(`/api/messages?room_id=${encodeURIComponent(state.activeChannel.id)}`))()"
        )["messages"][-1]
        assert stored["body"] == "" and stored["encrypted"] == 1 and stored["ciphertext"]
        assert secret not in json.dumps(stored)
        wait_value(alice, "(async()=>(await listClientOutbox()).length===0)()")
        offline_secret = "queued securely while browser connection is unavailable"
        queued = alice.evaluate(
            "(async()=>{"
            "const liveSocket=state.socket;state.socket=null;"
            f"document.querySelector('#message-input').value={json.dumps(offline_secret)};"
            "await sendMessage({preventDefault(){}});"
            "const records=await listClientOutbox();"
            "const result={id:records[0]?.id,count:records.length,serialized:JSON.stringify(records),"
            "status:document.querySelector('.message-delivery.pending')?.textContent||''};"
            "state.socket=liveSocket;return result;"
            "})()"
        )
        assert queued["count"] == 1 and queued["id"]
        assert offline_secret not in queued["serialized"]
        assert "Очікує" in queued["status"]
        alice.evaluate("flushClientOutbox()")
        wait_value(
            bob,
            f"document.querySelector('#message-list').innerText.includes({json.dumps(offline_secret)})",
            20,
        )
        wait_value(alice, "(async()=>(await listClientOutbox()).length===0)()", 20)
        wait_value(
            alice,
            f"document.querySelector('.message[data-message-id={json.dumps(queued['id'])}] .message-delivery.read')"
            "?.textContent.includes('Прочитано')",
            20,
        )
        delayed_order = bob.evaluate(
            "(async()=>{"
            "await addMessage({id:'delayed-ui-test',author_name:'Delayed',body:'old',encrypted:0,"
            "created_at:Date.now()/1000-345600,lamport:999999,origin_node:'node-old'});"
            "return {ids:[...document.querySelectorAll('#message-list .message')].map(item=>item.dataset.messageId),"
            "dates:[...document.querySelectorAll('#message-list .message-date-separator')].map(item=>item.textContent.trim())};"
            "})()"
        )
        assert delayed_order["ids"][0] == "delayed-ui-test"
        assert delayed_order["dates"][-1] == "Сьогодні" and delayed_order["dates"][0] != "Сьогодні"

        document_id = alice.evaluate(
            "(async()=>{"
            "const data=new FormData();"
            "data.append('file',new File(['Nexus presentation\\n'.repeat(120)],'browser-slides.txt',{type:'text/plain'}));"
            "data.append('channel_id',state.activeChannel.id);"
            "const result=await api('/api/documents',{method:'POST',body:data});"
            "if(!state.documents.some(item=>item.id===result.document.id))state.documents.unshift(result.document);"
            "state.selectedRecipients=new Set(['crypto_bob']);"
            "await openDocument(result.document);startPresentation();return result.document.id"
            "})()"
        )
        assert document_id
        wait_value(
            bob,
            "document.querySelector('#stage').classList.contains('viewer-mode')"
            "&&document.querySelector('#stage-name').textContent==='browser-slides.txt'",
            20,
        )
        assert bob.evaluate("document.querySelector('#current-page').disabled") is True
        assert bob.evaluate("document.querySelector('#page-prev').disabled") is True
        assert bob.evaluate("document.querySelector('#page-controls').classList.contains('hidden')") is True
        assert bob.evaluate("document.querySelector('#share-document').classList.contains('hidden')") is True
        assert bob.evaluate("document.querySelector('#close-stage').classList.contains('hidden')") is True
        assert bob.evaluate("document.querySelector('#document-viewer').classList.contains('pdf-document')") is False
        wait_value(bob, "document.querySelector('#document-viewer').classList.contains('remote-fitted')")
        assert bob.evaluate(
            "(()=>{const viewer=document.querySelector('#document-viewer');"
            "const frame=viewer.getBoundingClientRect();"
            "const shell=document.querySelector('#viewer-shell').getBoundingClientRect();"
            "const remote=state.remotePresentation;"
            "return remote.viewportWidth>0&&remote.viewportHeight>0"
            "&&viewer.offsetWidth===remote.viewportWidth&&viewer.offsetHeight===remote.viewportHeight"
            "&&frame.width<=shell.width+1&&frame.height<=shell.height+1"
            "&&Math.abs(frame.width/frame.height-remote.viewportWidth/remote.viewportHeight)<.01})()"
        ) is True

        bob.evaluate("updatePage(3)")
        assert bob.evaluate("state.page") == 1
        bob.evaluate("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))")
        assert bob.evaluate("document.querySelector('#stage').classList.contains('hidden')") is False

        alice.evaluate(
            "(()=>{const s=document.querySelector('#presentation-surface');"
            "for(let i=0;i<4;i++)s.dispatchEvent(new WheelEvent('wheel',{deltaY:120,deltaMode:0,cancelable:true}))})()"
        )
        wait_value(alice, "document.querySelector('#document-viewer').scrollTop>0")
        wait_value(bob, "document.querySelector('#document-viewer').scrollTop>0")
        alice_scroll = alice.evaluate("presentationScrollRatio()")
        wait_value(bob, f"Math.abs(presentationScrollRatio()-{alice_scroll})<.03")
        bob_scroll = bob.evaluate("presentationScrollRatio()")
        assert abs(alice_scroll - bob_scroll) < 0.03

        semantic_offset = alice.evaluate(
            "(()=>{const s=document.querySelector('#presentation-surface');"
            "const text=document.querySelector('.text-document');"
            "const viewer=document.querySelector('#document-viewer').getBoundingClientRect();"
            "const targetY=viewer.top+viewer.height*.25;let best=null;"
            "for(let offset=0;offset<text.textContent.length;offset++){"
            "const point=textPointFromOffset(text,offset);"
            "if(!point||point.x<viewer.left||point.x>viewer.right||point.y<viewer.top||point.y>viewer.bottom)continue;"
            "const distance=Math.abs(point.y-targetY);"
            "if(!best||distance<best.distance)best={offset,point,distance};}"
            "if(!best)return -1;"
            "window.__e2eTextPoint={offset:best.offset,x:best.point.x,y:best.point.y};"
            "s.dispatchEvent(new PointerEvent('pointermove',"
            "{clientX:best.point.x,clientY:best.point.y,bubbles:true}));return best.offset})()"
        )
        assert semantic_offset >= 0
        wait_value(
            bob,
            "!document.querySelector('#remote-cursor').classList.contains('hidden')"
            "&&state.remotePresentation?.cursor?.space==='text'"
            "&&Number.isInteger(state.remotePresentation.cursor.text_offset)",
        )
        assert bob.evaluate(
            "(()=>{const payload=state.remotePresentation.cursor;"
            "const point=textPointFromOffset(document.querySelector('.text-document'),payload.text_offset);"
            "const shell=document.querySelector('#viewer-shell').getBoundingClientRect();"
            "const cursor=document.querySelector('#remote-cursor');"
            "return point&&Math.abs(parseFloat(cursor.style.left)-(point.x-shell.left))<3"
            "&&Math.abs(parseFloat(cursor.style.top)-(point.y-shell.top))<3})()"
        ) is True
        viewer_scroll_before_cursor_moves = bob.evaluate("document.querySelector('#document-viewer').scrollTop")
        alice.evaluate(
            "(()=>{const s=document.querySelector('#presentation-surface');"
            "const text=document.querySelector('.text-document').getBoundingClientRect();"
            "const viewer=document.querySelector('#document-viewer').getBoundingClientRect();"
            "const x=Math.min(text.right-24,viewer.right-24);"
            "s.dispatchEvent(new PointerEvent('pointermove',"
            "{clientX:x,clientY:window.__e2eTextPoint.y,bubbles:true}))})()"
        )
        wait_value(
            bob,
            "state.remotePresentation?.cursor?.space==='text'"
            "&&!Number.isInteger(state.remotePresentation.cursor.text_offset)",
        )
        assert bob.evaluate(
            "(()=>{const payload=state.remotePresentation.cursor;"
            "const viewer=document.querySelector('#document-viewer').getBoundingClientRect();"
            "const shell=document.querySelector('#viewer-shell').getBoundingClientRect();"
            "const cursor=document.querySelector('#remote-cursor');"
            "return Math.abs(parseFloat(cursor.style.left)-(viewer.left+payload.viewport_x*viewer.width-shell.left))<3"
            "&&Math.abs(parseFloat(cursor.style.top)-(viewer.top+payload.viewport_y*viewer.height-shell.top))<3})()"
        ) is True
        viewer_scroll_after_cursor_moves = bob.evaluate("document.querySelector('#document-viewer').scrollTop")
        assert abs(viewer_scroll_after_cursor_moves - viewer_scroll_before_cursor_moves) < 1
        alice.evaluate(
            "(()=>{const s=document.querySelector('#presentation-surface');"
            "const point=textPointFromOffset(document.querySelector('.text-document'),window.__e2eTextPoint.offset);"
            "s.dispatchEvent(new PointerEvent('pointermove',"
            "{clientX:point.x,clientY:point.y,bubbles:true}))})()"
        )
        wait_value(bob, "Number.isInteger(state.remotePresentation?.cursor?.text_offset)")
        cursor_before_scroll = bob.evaluate("state.remotePresentation.cursor.text_offset")
        alice.evaluate(
            "(()=>{const s=document.querySelector('#presentation-surface');"
            "s.dispatchEvent(new WheelEvent('wheel',{deltaY:260,deltaMode:0,cancelable:true}))})()"
        )
        wait_value(bob, f"state.remotePresentation.cursor.text_offset>{cursor_before_scroll}")
        assert bob.evaluate(
            "(()=>{const cursor=document.querySelector('#remote-cursor');"
            "const viewer=document.querySelector('#document-viewer').getBoundingClientRect();"
            "const shell=document.querySelector('#viewer-shell').getBoundingClientRect();"
            "const expected=state.remotePresentation.cursor.viewport_y;"
            "return Math.abs((parseFloat(cursor.style.top)+shell.top-viewer.top)/viewer.height-expected)<.04})()"
        ) is True
        text_width_before_zoom = alice.evaluate(
            "document.querySelector('.text-document').getBoundingClientRect().width"
        )
        alice.evaluate("document.querySelector('#zoom-in').click();document.querySelector('#zoom-in').click()")
        wait_value(bob, "state.documentZoom===1.2&&document.querySelector('#zoom-reset').textContent==='120%'")
        text_width_after_zoom = alice.evaluate(
            "document.querySelector('.text-document').getBoundingClientRect().width"
        )
        assert abs(text_width_after_zoom / text_width_before_zoom - 1.2) < 0.03
        assert bob.evaluate("document.querySelector('#zoom-in').disabled&&document.querySelector('#zoom-out').disabled") is True
        alice.evaluate(
            "(()=>{const s=document.querySelector('#presentation-surface');const r=s.getBoundingClientRect();"
            "s.dispatchEvent(new PointerEvent('pointermove',{clientX:r.left+r.width*.45,clientY:r.top+r.height*.3,bubbles:true}))})()"
        )
        wait_value(bob, "state.remotePresentation?.cursor?.space==='text'&&state.remotePresentation.zoom===1.2")
        alice.evaluate("stopPresentation()")
        wait_value(bob, "document.querySelector('#stage').classList.contains('hidden')")
        assert alice.evaluate("document.querySelector('#stage').classList.contains('presenter-mode')") is False
        for browser in (alice, bob):
            assert browser.evaluate("window.scrollY") == 0
            assert browser.evaluate("document.documentElement.scrollTop") == 0
            assert browser.evaluate(
                "document.querySelector('.rail .brand-mark').getBoundingClientRect().top"
            ) >= 0
            assert browser.evaluate("document.querySelector('.topbar').getBoundingClientRect().top") >= 0

        pdf_id = alice.evaluate(
            "(async()=>{const data=new FormData();"
            "data.append('file',new File(['%PDF-1.4\\n1 0 obj << /Type /Page >> endobj\\n2 0 obj << /Type /Page >> endobj\\n'],"
            "'stable-slides.pdf',{type:'application/pdf'}));"
            "data.append('channel_id',state.activeChannel.id);"
            "const result=await api('/api/documents',{method:'POST',body:data});"
            "if(!state.documents.some(item=>item.id===result.document.id))state.documents.unshift(result.document);"
            "await openDocument(result.document);startPresentation();return result.document.id})()"
        )
        assert pdf_id
        wait_value(
            bob,
            "document.querySelector('#stage').classList.contains('viewer-mode')"
            "&&document.querySelector('#stage-name').textContent==='stable-slides.pdf'"
            "&&document.querySelector('#document-viewer iframe')!==null",
            20,
        )
        assert bob.evaluate("document.querySelector('#document-viewer').classList.contains('pdf-document')") is True
        assert bob.evaluate("document.querySelector('#page-controls').classList.contains('hidden')") is False
        assert bob.evaluate("getComputedStyle(document.querySelector('#document-viewer')).overflowY") == "auto"
        pdf_width_before_zoom = alice.evaluate(
            "document.querySelector('#document-viewer iframe').getBoundingClientRect().width"
        )
        alice.evaluate("document.querySelector('#zoom-in').click()")
        wait_value(bob, "state.documentZoom===1.1&&document.querySelector('#zoom-reset').textContent==='110%'")
        pdf_width_after_zoom = alice.evaluate(
            "document.querySelector('#document-viewer iframe').getBoundingClientRect().width"
        )
        assert abs(pdf_width_after_zoom / pdf_width_before_zoom - 1.1) < 0.03
        alice.evaluate("document.querySelector('#page-next').click()")
        wait_value(bob, "state.page===2&&document.querySelector('#current-page').value==='2'")
        bob.evaluate("document.querySelector('#document-viewer iframe').dataset.stability='preserved'")
        time.sleep(0.08)
        alice.evaluate(
            "(()=>{const s=document.querySelector('#presentation-surface');const r=s.getBoundingClientRect();"
            "for(let i=0;i<5;i++)s.dispatchEvent(new PointerEvent('pointermove',{clientX:r.left+r.width*(.4+i*.02),"
            "clientY:r.top+r.height*.35,bubbles:true}))})()"
        )
        wait_value(
            bob,
            "(()=>{const cursor=document.querySelector('#remote-cursor');"
            "return state.remotePresentation?.cursor?.space==='viewport'"
            "&&!cursor.classList.contains('hidden')})()",
        )
        assert bob.evaluate("document.querySelector('#document-viewer iframe').dataset.stability") == "preserved"

        alice.evaluate("stopPresentation()")
        wait_value(bob, "document.querySelector('#stage').classList.contains('hidden')")
        image_id = alice.evaluate(
            "(async()=>{const canvas=document.createElement('canvas');canvas.width=900;canvas.height=600;"
            "const context=canvas.getContext('2d');context.fillStyle='#3557d4';context.fillRect(0,0,900,600);"
            "context.fillStyle='white';context.font='64px sans-serif';context.fillText('Nexus image',220,320);"
            "const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/png'));const data=new FormData();"
            "data.append('file',new File([blob],'shared-image.png',{type:'image/png'}));"
            "data.append('channel_id',state.activeChannel.id);"
            "const result=await api('/api/documents',{method:'POST',body:data});"
            "if(!state.documents.some(item=>item.id===result.document.id))state.documents.unshift(result.document);"
            "await openDocument(result.document);startPresentation();return result.document.id})()"
        )
        assert image_id
        wait_value(
            bob,
            "document.querySelector('#stage').classList.contains('viewer-mode')"
            "&&document.querySelector('#document-viewer img')?.complete",
            20,
        )
        image_width_at_100 = alice.evaluate(
            "document.querySelector('#document-viewer img').getBoundingClientRect().width"
        )
        alice.evaluate("setDocumentZoom(2)")
        wait_value(bob, "state.documentZoom===2&&document.querySelector('#zoom-reset').textContent==='200%'")
        image_width_at_200 = alice.evaluate(
            "document.querySelector('#document-viewer img').getBoundingClientRect().width"
        )
        assert abs(image_width_at_200 / image_width_at_100 - 2) < 0.03
        assert alice.evaluate(
            "document.querySelector('#document-viewer').scrollWidth>"
            "document.querySelector('#document-viewer').clientWidth"
        ) is True
        assert alice.evaluate(
            "(()=>{const viewer=document.querySelector('#document-viewer').getBoundingClientRect();"
            "const surface=document.querySelector('#presentation-surface').getBoundingClientRect();"
            "return viewer.bottom-surface.bottom>=15})()"
        ) is True
        alice.evaluate(
            "(()=>{const viewer=document.querySelector('#document-viewer');"
            "viewer.scrollLeft=(viewer.scrollWidth-viewer.clientWidth)*.62})()"
        )
        wait_value(bob, "Math.abs(presentationScrollRatio('x')-.62)<.04")
        assert abs(
            alice.evaluate("presentationScrollRatio('x')")
            - bob.evaluate("presentationScrollRatio('x')")
        ) < 0.04
        alice.evaluate("setDocumentZoom(.5)")
        wait_value(bob, "state.documentZoom===0.5&&document.querySelector('#zoom-reset').textContent==='50%'")
        image_width_at_50 = alice.evaluate(
            "document.querySelector('#document-viewer img').getBoundingClientRect().width"
        )
        assert abs(image_width_at_50 / image_width_at_100 - 0.5) < 0.03
        alice.evaluate("setDocumentZoom(1.3)")
        wait_value(bob, "state.documentZoom===1.3&&document.querySelector('#zoom-reset').textContent==='130%'")
        alice.evaluate(
            "(()=>{const image=document.querySelector('#document-viewer img');const r=image.getBoundingClientRect();"
            "document.querySelector('#presentation-surface').dispatchEvent(new PointerEvent('pointermove',"
            "{clientX:r.left+r.width*.5,clientY:r.top+r.height*.5,bubbles:true}))})()"
        )
        wait_value(bob, "state.remotePresentation?.cursor?.space==='image'")
        assert bob.evaluate(
            "(()=>{const image=document.querySelector('#document-viewer img').getBoundingClientRect();"
            "const shell=document.querySelector('#viewer-shell').getBoundingClientRect();"
            "const cursor=document.querySelector('#remote-cursor');return Math.abs(parseFloat(cursor.style.left)-"
            "(image.left+image.width*.5-shell.left))<3&&Math.abs(parseFloat(cursor.style.top)-"
            "(image.top+image.height*.5-shell.top))<3})()"
        ) is True

        alice.evaluate(
            "state.presenting=false;clearInterval(state.presentationHeartbeatTimer);"
            "state.presentationHeartbeatTimer=null"
        )
        wait_value(bob, "document.querySelector('#stage').classList.contains('hidden')", 10)
        assert bob.evaluate("window.scrollY") == 0
        assert bob.evaluate("document.querySelector('.rail .brand-mark').getBoundingClientRect().top") >= 0
        assert not alice.exceptions and not bob.exceptions
        print(
            "OK: cross-node E2EE plus presenter-controlled page/cursor synchronization "
            "and graceful/timeout remote-stage cleanup"
        )
    finally:
        alice.close()
        bob.close()


if __name__ == "__main__":
    main()

-- L1-L12 from the plan, except L8: Tern replaces invalid UTF-8 during encoding.
-- Each accepted snapshot is followed by a fresh fixture; L12 uses only the reconciliation clock.
local function one_pane(pane, title, tab)
	H.world = H.new_world()
	H.world.agents = { { pane = pane, state = "idle", kind = "agent", program = "omp", host = 0, alive = true } }
	H.world.roles[pane] = "omp.session"
	H.world.panes = { { pane = pane, title = title, tab = tab } }
	H.world.tabs = if tab ~= nil then { { id = tab, title = "focused shell title" } } else {}
end

local function snapshot(first)
	if first then
		H.boot()
		H.complete(0, H.REG_OK)
	else
		H.handlers.session_ended({}, H.cx)
		H.advance(75)
	end
	assert(#H.runs == 1 and H.op(H.runs[1]) == "snapshot", "expected one snapshot")
	local payload = H.payload(H.runs[1])
	assert(payload.coverage == "complete", "label lookup must not degrade coverage")
	return payload
end

local function observation(payload, pane)
	for _, row in ipairs(payload.observations) do
		if row.pane == pane then
			return row
		end
	end
	error("missing observation for pane " .. pane)
end

-- Keep the byte budget honest: the fake encoder follows the probed Tern encoding.
local encoding_probe = tern.json.encode({ text = "\t\1\127é", pane = 949187772435 })
assert(encoding_probe:find("\\t", 1, true) and encoding_probe:find("\\u0001", 1, true), "C0 controls must be escaped")
assert(encoding_probe:find("\127é", 1, true), "DEL and UTF-8 must remain raw")
assert(encoding_probe:find("949187772435", 1, true), "pane ids must encode as exact integers")

-- L1: a user-given tab name wins over the pane's title.
one_pane(11, "Own pane title", 101)
H.world.tabs[1].name = "My work"
local named = snapshot(true)
assert(observation(named, 11).label == "My work", "L1: user-given tab name must label the OMP pane")
assert(named.diag.label_tab == 1, "L1: one tab label")
H.complete(0, H.SNAP_OK)
print("L1  user-given tab name wins")

-- L2: the raw pane title retains the OMP glyph; the coordinator sanitizes it.
local glyph_title = "\u{F1517} DOCS: Review docs"
one_pane(12, glyph_title, 102)
local unnamed = snapshot()
assert(observation(unnamed, 12).label == glyph_title, "L2: preserve the raw OMP pane title and glyph")
assert(unnamed.diag.label_title == 1, "L2: one pane-title label")
H.complete(0, H.SNAP_OK)
print("L2  raw glyph and pane title preserved")

-- L3: the focused shell sets TabInfo.title, not the OMP pane's row label.
one_pane(13, "OMP's own title", 103)
H.world.tabs[1].title = "zsh"
table.insert(H.world.panes, { pane = 130, title = "zsh", tab = 103 })
local split = snapshot()
assert(observation(split, 13).label == "OMP's own title", "L3: never label from TabInfo.title or the focused shell")
H.complete(0, H.SNAP_OK)
print("L3  focused shell title ignored")

-- L4: a whitespace-only tab name does not hide the pane title.
one_pane(14, "Pane fallback", 104)
H.world.tabs[1].name = "   "
local whitespace = snapshot()
assert(observation(whitespace, 14).label == "Pane fallback", "L4: whitespace-only tab name falls back to pane title")
H.complete(0, H.SNAP_OK)
print("L4  whitespace-only name falls back")

-- L5: parked panes have no tab, but still have their own title.
one_pane(15, "Parked OMP", nil)
local parked = snapshot()
assert(observation(parked, 15).label == "Parked OMP", "L5: parked pane keeps its title")
H.complete(0, H.SNAP_OK)
print("L5  parked pane title preserved")

-- L6: an OMP pane missing from panes() has no invented label.
one_pane(16, "Not returned", 106)
H.world.panes = {}
local missing = snapshot()
assert(observation(missing, 16).label == nil, "L6: omit unknown label")
assert(missing.diag.label_none == 1, "L6: one unknown label")
H.complete(0, H.SNAP_OK)
print("L6  missing pane has no label")

-- L7: 300-byte titles cut to their maximal complete-character prefix.
H.world = H.new_world()
local characters = { "é", "€", "😀" }
local titles = {}
for i, char in ipairs(characters) do
	local pane = 70 + i
	local title = string.rep(char, 300 / #char)
	titles[pane] = title
	table.insert(H.world.agents, { pane = pane, state = "idle", kind = "agent", program = "omp", host = 0, alive = true })
	H.world.roles[pane] = "omp.session"
	table.insert(H.world.panes, { pane = pane, title = title, tab = 107 })
end
H.world.tabs = { { id = 107, title = "not a label" } }
local bounded = snapshot()
for i, char in ipairs(characters) do
	local pane = 70 + i
	local label = observation(bounded, pane).label
	local expected_bytes = math.floor(256 / #char) * #char
	assert(label == titles[pane]:sub(1, expected_bytes), "L7: maximal UTF-8 prefix for " .. #char .. "-byte characters")
	assert(#label <= 256 and utf8.len(label) ~= nil, "L7: bounded valid UTF-8 label")
end
H.complete(0, H.SNAP_OK)
print("L7  UTF-8 prefix bytes: 256 255 256")

-- L9: unavailable optional APIs never suppress status or invent labels.
one_pane(19, "Available title", 109)
H.world.fail.panes = true
local before_panes, before_tabs = H.calls.panes, H.calls.tabs
local no_panes = snapshot()
assert(observation(no_panes, 19).label == nil, "L9: panes failure omits labels")
assert(no_panes.diag.labels_unavailable == 1, "L9: one unavailable label source")
assert(H.calls.panes == before_panes + 1 and H.calls.tabs == before_tabs, "L9: panes failure must skip tabs()")
H.complete(0, H.SNAP_OK)
H.world.fail.panes, H.world.fail.tabs = false, true
before_panes, before_tabs = H.calls.panes, H.calls.tabs
local no_tabs = snapshot()
assert(observation(no_tabs, 19).label == "Available title", "L9: tabs failure still uses pane titles")
assert(no_tabs.diag.labels_unavailable == 1, "L9: one failed tabs lookup")
assert(H.calls.panes == before_panes + 1 and H.calls.tabs == before_tabs + 1, "L9: each optional API called once")
H.complete(0, H.SNAP_OK)
print("L9  panes/tabs failures retain complete status coverage")

-- L10: API work is per snapshot, not per OMP pane, and is skipped when empty.
H.world = H.new_world()
for i = 1, 3 do
	local pane = 100 + i
	table.insert(H.world.agents, { pane = pane, state = "working", kind = "agent", program = "omp", host = 0, alive = true })
	H.world.roles[pane] = "omp.session"
	table.insert(H.world.panes, { pane = pane, title = "OMP " .. i, tab = if i < 3 then 201 else 202 })
end
H.world.tabs = { { id = 201, name = "First" }, { id = 202, name = "Second" } }
before_panes, before_tabs = H.calls.panes, H.calls.tabs
local three = snapshot()
assert(#three.observations == 3, "L10: all three OMP panes observed")
assert(observation(three, 101).label == "First" and observation(three, 102).label == "First" and observation(three, 103).label == "Second", "L10: labels join by tab id")
assert(H.calls.panes == before_panes + 1 and H.calls.tabs == before_tabs + 1, "L10: exactly one panes() and tabs() per pass")
H.complete(0, H.SNAP_OK)
H.world.roles = {}
before_panes, before_tabs = H.calls.panes, H.calls.tabs
local empty = snapshot()
assert(#empty.observations == 0, "L10: no OMP observations remain")
assert(H.calls.panes == before_panes and H.calls.tabs == before_tabs, "L10: empty pass makes no label API calls")
H.complete(0, H.SNAP_OK)
print("L10  one lookup per populated pass; zero when empty")

-- L11: use the probed remote host shape; invalid addresses may fall back to name.
H.world = H.new_world()
H.world.hosts = {
	{ id = 0, kind = "local", state = "connected", address = "ignored-local-address" },
	{ id = 1, kind = "remote", state = "connected", address = "sf-studio.tailfb479a.ts.net", name = "sf-studio.tailfb479a.ts.net" },
	{ id = 2, kind = "remote", state = "connected", name = "name-only.example" },
	{ id = 3, kind = "remote", state = "connected" },
	{ id = 4, kind = "remote", state = "connected", address = "bad\naddress", name = "usable-name.example" },
}
local host_expectations = { "local", "sf-studio.tailfb479a.ts.net", "name-only.example", "id:3", "usable-name.example" }
for i, host in ipairs(H.world.hosts) do
	local pane = 110 + i
	host.panes = { pane }
	table.insert(H.world.agents, { pane = pane, state = "idle", kind = "agent", program = "omp", host = host.id, alive = true })
	H.world.roles[pane] = "omp.session"
	table.insert(H.world.panes, { pane = pane, title = "Host sample" })
end
local hosts = snapshot()
assert(#hosts.observations == 5, "L11: all connected host panes observed")
for i, expected in ipairs(host_expectations) do
	assert(observation(hosts, 110 + i).host == expected, "L11: wrong host key for host " .. (i - 1))
end
assert(hosts.diag.host_fallback == 1, "L11: only the nameless remote needs an id fallback")
H.complete(0, H.SNAP_OK)
print("L11  local, remote address/name, and id fallback keys")

-- L12: a rename needs no title event; one reconciliation refreshes the label.
one_pane(121, "Own pane title", 212)
H.world.tabs[1].name = "Before rename"
local before_rename = snapshot()
assert(observation(before_rename, 121).label == "Before rename", "L12: initial tab name")
H.complete(0, H.SNAP_OK)
H.world.tabs[1].name = "After rename"
assert(H.handlers.title == nil, "L12: no title-event subscription")
local rename_wait = H.wait_for_run(6000)
assert(rename_wait <= 5000 and H.op(H.runs[1]) == "snapshot", "L12: next reconciliation within 5 s")
local renamed = H.payload(H.runs[1])
assert(observation(renamed, 121).label == "After rename", "L12: next reconciliation carries renamed tab")
H.complete(0, H.SNAP_OK)
print("L12  renamed tab refreshes within one 5 s pass")
print("OK")

-- B1-B3: real encoded byte budgets, label fallback, and encode-failure recovery.
local SNAPSHOT_BUDGET = 192 * 1024

local function next_snapshot()
	H.handlers.session_ended({}, H.cx)
	H.advance(75)
	assert(#H.runs == 1 and H.op(H.runs[1]) == "snapshot", "expected one snapshot helper")
	return H.runs[1], H.payload(H.runs[1])
end

local function single_pane(title)
	H.world = H.new_world()
	H.world.agents = { { pane = 601, state = "idle", kind = "agent", program = "omp", host = 0, alive = true } }
	H.world.roles[601] = "omp.session"
	H.world.panes = { { pane = 601, title = title, tab = 1 } }
	H.world.tabs = { { id = 1, title = "not a label" } }
end

-- B1: labels put this complete snapshot over budget; one label-free retry fits.
local long_host = string.rep("h", 128)
local long_label = string.rep("L", 255)
H.world.hosts = { { id = 9, kind = "remote", state = "connected", address = long_host, name = long_host, panes = {} } }
for pane = 1, 500 do
	table.insert(H.world.agents, { pane = pane, state = "idle", kind = "agent", program = "omp", host = 9, alive = true })
	H.world.roles[pane] = "omp.session"
	table.insert(H.world.panes, { pane = pane, title = long_label, tab = 1 })
	table.insert(H.world.hosts[1].panes, pane)
end
H.world.tabs = { { id = 1, title = "not a label" } }
H.boot()
H.complete(0, H.REG_OK)
assert(#H.runs == 1 and H.op(H.runs[1]) == "snapshot", "B1: activating snapshot must be sent")
local large_run = H.runs[1]
local large = H.payload(large_run)
assert(large.coverage == "complete" and #large.observations == 500, "B1: budget fallback preserves complete status coverage")
assert(large.diag.labels_dropped == 1, "B1: oversized labelled snapshot must drop labels once")
for _, row in ipairs(large.observations) do
	assert(row.label == nil, "B1: no label may survive the fallback")
	assert(row.host == long_host, "B1: retain the full 128-byte host key")
end
local largest_attempt = 0
for text, payload in pairs(H.encoded) do
	if payload.observations ~= nil then
		largest_attempt = math.max(largest_attempt, #text)
	end
end
assert(largest_attempt > SNAPSHOT_BUDGET, "B1: fixture must exercise an oversized encoded snapshot")
assert(#large_run.opts.stdin <= SNAPSHOT_BUDGET, "B1: sent snapshot must fit the 192 KiB budget")
print("B1  encoded bytes before/after dropping labels:", largest_attempt, #large_run.opts.stdin, "observations:", #large.observations)
H.complete(0, H.SNAP_OK)

-- B2: an encoder that rejects labels still sends status, then accepts another pass.
single_pane("Label rejected by encoder")
local label_rejections = 0
H.world.fail.encode = function(payload)
	for _, row in ipairs(payload.observations or {}) do
		if row.label ~= nil then
			label_rejections += 1
			return true
		end
	end
	return false
end
local before_attempts = #H.payloads
local fallback_run, fallback = next_snapshot()
assert(label_rejections == 1 and #H.payloads == before_attempts + 2, "B2: encode with labels once, then retry without them once")
assert(fallback.coverage == "complete" and #fallback.observations == 1 and fallback.observations[1].label == nil, "B2: send the label-free snapshot")
assert(#fallback_run.opts.stdin <= SNAPSHOT_BUDGET, "B2: fallback fits the byte budget")
H.complete(0, H.SNAP_OK)
H.wait_for_run(6000)
assert(#H.runs == 1 and H.op(H.runs[1]) == "snapshot", "B2: completion must clear in_flight for the next reconciliation")
assert(H.payload(H.runs[1]).observations[1].label == nil, "B2: reconciliation still falls back without labels")
H.complete(0, H.SNAP_OK)
print("B2  labelled encode throws; fallback and next reconciliation both send status")

-- B3: an encoder that always throws cannot wedge either commands or snapshots.
single_pane("Recovered label")
H.world.fail.encode = true
local before_logs, before_toasts = #H.logs, #H.toasts
H.commands.doctor.run(H.cx)
assert(#H.runs == 0, "B3: a failed command encode must not spawn")
assert(#H.toasts == before_toasts + 1 and H.toasts[#H.toasts]:find("encode_failed", 1, true), "B3: run_helper reports synchronous encode_failed through the command cx")
H.handlers.session_ended({}, H.cx)
H.advance(75)
assert(#H.runs == 0, "B3: failed snapshot encoding must not spawn")
before_attempts = #H.payloads
H.advance(10000)
assert(#H.runs == 0 and #H.payloads > before_attempts, "B3: later passes retry encoding without an in_flight wedge")
local encode_warnings = 0
for i = before_logs + 1, #H.logs do
	local line = H.logs[i]
	if line:sub(1, 4) == "warn" and line:find("encode_failed", 1, true) then
		encode_warnings += 1
	end
end
assert(encode_warnings == 1, "B3: repeated encoding failures produce exactly one warning")
H.world.fail.encode = false
H.wait_for_run(35000)
assert(#H.runs == 1 and H.op(H.runs[1]) == "snapshot", "B3: recovery sends a snapshot without re-registering")
local recovered_payload = H.payload(H.runs[1])
assert(recovered_payload.coverage == "complete" and recovered_payload.observations[1].label == "Recovered label", "B3: recovery restores the real label and status")
H.complete(0, H.SNAP_OK)
next_snapshot()
H.complete(0, H.SNAP_OK)
print("B3  one warning across repeated encode failures; recovery and next pass succeed")
print("OK")

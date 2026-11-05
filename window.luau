--!nonstrict
-- tern-sidepulse window observer.
--
-- Every Tern window runs one copy. It enumerates the OMP sessions the window can see, projects
-- them to a small status snapshot (pane id, state, outcome notice) and hands it to the Python
-- helper, which forwards it to the one local coordinator that talks to SidePulse. It never
-- submits prompts, approves anything, reads transcripts or changes layouts; it keeps only plain
-- data between callbacks, never a `cx`, a row or a timer handle.

local VERSION = "0.1.0"
local DATA = tern.plugin.data
local BRIDGE = tern.plugin.dir .. "/bridge.py"
local COALESCE_MS = 75
local RECON_MS = 5000
local HELPER_TIMEOUT_MS = 4000
local MAX_OBSERVATIONS = 512
local MAX_BACKOFF_MS = 30000
local SURFACE_OPTS = { nodes = 1, depth = 1 } -- the cheapest documented header query; never max_chars alone

local PYTHON_CANDIDATES = {
	"/opt/homebrew/bin/python3",
	"/usr/local/bin/python3",
	"/opt/homebrew/bin/python3.14",
	"/opt/homebrew/bin/python3.13",
	"/opt/homebrew/bin/python3.12",
	"/opt/homebrew/bin/python3.11",
	"/opt/homebrew/bin/python3.10",
	"/usr/local/bin/python3.14",
	"/usr/local/bin/python3.13",
	"/usr/local/bin/python3.12",
	"/usr/local/bin/python3.11",
	"/usr/local/bin/python3.10",
	"/Library/Frameworks/Python.framework/Versions/Current/bin/python3",
}
-- /usr/bin/python3 is a stub that opens an install dialog without the Command Line Tools.
local CLT_PYTHON = "/Library/Developer/CommandLineTools/usr/bin/python3"

local FENCE_ERRORS = {
	epoch_mismatch = true,
	expired_generation = true,
	stale_generation = true,
	unknown_token = true,
	seq_not_increasing = true,
	version_mismatch = true,
}

local S = {
	booted = false,
	inert = nil :: string?, -- a missing capability keeps the observer inert; doctor reports it
	window_key = "",
	python = nil :: string?,
	python_source = "none",
	rejected = {} :: { [string]: boolean },
	epoch = nil :: string?,
	generation = nil :: number?,
	seq = 0,
	ttl = { done = 20000, failed = 35000 }, -- notice_seconds * 1000 + 5000, refreshed on register
	in_flight = false,
	dirty = false,
	flush_armed = false,
	recon_armed = false,
	fail_count = 0,
	fence_errors = 0,
	retry_at = 0,
	failing = false,
	disabled = false,
	omp = {} :: { [number]: boolean }, -- classification cache: pane -> true
	notices = {} :: { [number]: { id: string, kind: string, at: number } },
	notice_seq = 0,
	diag = {} :: { [string]: number }, -- classification counts of the latest enumeration pass
	stats = {} :: { [string]: number }, -- cumulative for this VM: budget_pauses, helper_failures
	last_error = nil :: string?,
	last_ack = nil :: string?,
}

local function now_ms(): number
	return tern.now()
end

local function count(key: string)
	S.diag[key] = (S.diag[key] or 0) + 1
end

local function tally(key: string)
	S.stats[key] = (S.stats[key] or 0) + 1
end

local function diag_report(): { [string]: number }
	local merged = {}
	for key, value in pairs(S.diag) do
		merged[key] = value
	end
	for key, value in pairs(S.stats) do
		merged[key] = value
	end
	return merged
end

local function warn_once(code: string, detail: string?)
	-- One line when delivery starts failing, one when it recovers; never per retry.
	if not S.failing then
		S.failing = true
		tern.log.warn("tern-sidepulse helper failing", code, detail or "")
	end
	S.last_error = code
end

local function recovered()
	if S.failing then
		S.failing = false
		tern.log.info("tern-sidepulse helper recovered")
	end
	S.last_error = nil
end

local function backoff(code: string, detail: string?)
	S.fail_count += 1
	S.retry_at = now_ms() + math.min(MAX_BACKOFF_MS, 500 * 2 ^ S.fail_count)
	tally("helper_failures")
	warn_once(code, detail)
end

-- ---- Python resolution --------------------------------------------------------------------

local function read_config(): { [string]: any }
	local ok, text = pcall(tern.fs.read, DATA .. "/config.json", 65536)
	if not ok then
		return {}
	end
	local ok2, decoded = pcall(tern.json.decode, text)
	if ok2 and type(decoded) == "table" then
		return decoded
	end
	return {}
end

local function resolve_python()
	local config = read_config()
	local override = config.python
	if type(override) == "string" and override:sub(1, 1) == "/" and not S.rejected[override] and tern.fs.exists(override) then
		S.python, S.python_source = override, "config"
		return
	end
	for _, candidate in ipairs(PYTHON_CANDIDATES) do
		if not S.rejected[candidate] and tern.fs.exists(candidate) then
			S.python, S.python_source = candidate, "candidate"
			return
		end
	end
	if not S.rejected["/usr/bin/python3"] and tern.fs.exists(CLT_PYTHON) then
		S.python, S.python_source = "/usr/bin/python3", "clt"
		return
	end
	S.python, S.python_source = nil, "none"
end

-- ---- enumeration ---------------------------------------------------------------------------

local function is_disabled(): boolean
	return tern.fs.exists(DATA .. "/disabled")
end

local function num(value: any): number?
	if type(value) == "number" then
		return value
	end
	return nil
end

local function str(value: any): string?
	if type(value) == "string" then
		return value
	end
	return nil
end

-- Returns coverage ("complete" | "unknown") and the observation list. Reads only pane, state,
-- kind, program, host and alive from each row; titles, cwd, model and messages are never touched.
local function enumerate(cx: WindowCx): (string, { any })
	S.diag = {}
	local ok_agents, agents = pcall(function()
		return cx.agents:list()
	end)
	local ok_hosts, hosts = pcall(function()
		return cx.hosts:list()
	end)
	if not ok_agents or type(agents) ~= "table" or not ok_hosts or type(hosts) ~= "table" then
		return "unknown", tern.json.array({})
	end

	local host_state: { [number]: string } = {}
	local pane_host: { [number]: number } = {}
	local only_host: number? = nil
	local host_count = 0
	for _, host in ipairs(hosts :: { any }) do
		local id = num(host.id)
		if id ~= nil then
			host_count += 1
			host_state[id] = str(host.state) or "unknown"
			if type(host.panes) == "table" then
				for _, pane in ipairs(host.panes) do
					local pid = num(pane)
					if pid ~= nil then
						pane_host[pid] = id
					end
				end
			end
			if host.kind == "local" then
				only_host = id
			end
		end
	end
	if host_count ~= 1 then
		only_host = nil
	end

	local seen: { [number]: boolean } = {}
	local observations = {}
	for _, row in ipairs(agents :: { any }) do
		local pane = num(row.pane)
		if pane ~= nil then
			seen[pane] = true
			local kind = str(row.kind)
			local program = str(row.program)
			if kind ~= nil and kind ~= "agent" then
				count("non_omp")
				S.omp[pane] = nil
				S.notices[pane] = nil
			else
				if program == nil then
					count("program_missing")
				elseif program ~= "omp" then
					count("wrapped") -- OMP under env/npx/a script: the surface role still decides
				end
				local state = str(row.state) or "unknown"
				if row.alive == false then
					state = "exited"
				end
				if state == "exited" or state == "closed" then
					count("exited")
					S.omp[pane] = nil
					S.notices[pane] = nil
				else
					local host = num(row.host) or pane_host[pane] or only_host
					if host == nil then
						count("unmapped")
						S.notices[pane] = nil
					elseif host_state[host] ~= "connected" then
						count("host_unavailable")
						S.notices[pane] = nil
					else
						if not S.omp[pane] then
							local ok_surface, tree = pcall(function()
								return cx.session:surface(pane, SURFACE_OPTS)
							end)
							if ok_surface and type(tree) == "table" and type(tree.surface) == "table" and tree.surface.role == "omp.session" then
								S.omp[pane] = true
							end
						end
						if not S.omp[pane] then
							count("unclassified")
						else
							count("omp")
							if state ~= "idle" and state ~= "working" and state ~= "waiting_input" then
								count("unknown_state")
								state = "unknown"
							end
							local notice = S.notices[pane]
							local sent_notice = nil
							if notice ~= nil then
								if now_ms() - notice.at > (S.ttl[notice.kind] or S.ttl.done) then
									S.notices[pane] = nil
								elseif state == "working" or state == "waiting_input" then
									S.notices[pane] = nil
								elseif state == "idle" then
									sent_notice = { id = notice.id, kind = notice.kind }
								end
							end
							table.insert(observations, { pane = pane, state = state, notice = sent_notice })
						end
					end
				end
			end
		end
	end
	for pane in pairs(S.omp) do
		if not seen[pane] then
			S.omp[pane] = nil
		end
	end
	for pane in pairs(S.notices) do
		if not seen[pane] then
			S.notices[pane] = nil
		end
	end
	if #observations > MAX_OBSERVATIONS then
		count("too_many")
		return "unknown", tern.json.array({})
	end
	return "complete", tern.json.array(observations)
end

-- ---- helper invocation -----------------------------------------------------------------------

local function parse_result(result: ProcessResult): (any, string?)
	if result.timed_out then
		return nil, "helper_timeout"
	end
	local text = (result.stdout or ""):match("^%s*(.-)%s*$")
	if text == "" then
		return nil, "helper_failed"
	end
	local last = text:match("([^\n]*)$") or text
	local ok, decoded = pcall(tern.json.decode, last)
	if not ok or type(decoded) ~= "table" then
		return nil, "helper_failed"
	end
	return decoded, nil
end

local function run_helper(op: string, payload: any, cb: (any, string?, WindowCx?) -> ()): boolean
	if S.python == nil then
		resolve_python()
	end
	if S.python == nil then
		cb(nil, "python_unavailable", nil)
		return false
	end
	local argv = { S.python, "-I", "-B", BRIDGE, op, "--data-dir", DATA }
	local opts = { stdin = tern.json.encode(payload), env = { PYTHONDONTWRITEBYTECODE = "1" }, timeout_ms = HELPER_TIMEOUT_MS }
	local ok, _err = pcall(tern.process.run, argv, opts, function(result: ProcessResult, cx: WindowCx?)
		local decoded, code = parse_result(result)
		if decoded == nil and result.status == 3 and S.python ~= nil then
			-- The interpreter is too old: refuse it for this VM and look again.
			S.rejected[S.python] = true
			resolve_python()
			code = "python_too_old"
		end
		cb(decoded, code, cx)
	end)
	if not ok then
		cb(nil, "spawn", nil)
		return false
	end
	return true
end

-- ---- pump: one call in flight, one newest snapshot ---------------------------------------

local pump

local function request_flush()
	if S.flush_armed then
		return
	end
	S.flush_armed = true
	tern.timer(COALESCE_MS, function(cx: WindowCx?)
		S.flush_armed = false
		S.dirty = true
		if cx then
			pump(cx)
		end
	end)
end

local function handle_error(code: string, body: any)
	if code == "disabled" then
		S.disabled = true
		S.generation = nil
		S.dirty = false
		recovered()
		return
	end
	if FENCE_ERRORS[code] then
		S.generation, S.epoch = nil, nil
		S.notices = {} -- never replay notices into a new generation
		S.fence_errors += 1
		if S.fence_errors >= 3 then
			backoff(code, nil)
		else
			S.dirty = true
		end
		return
	end
	local detail = nil
	if type(body) == "table" and type(body.detail) == "string" then
		detail = body.detail
	end
	backoff(code, detail)
end

local function on_register(body: any, code: string?, cx: WindowCx?)
	S.in_flight = false
	if body == nil then
		backoff(code or "helper_failed", nil)
	elseif body.ok == true and type(body.generation) == "number" and type(body.epoch) == "string" then
		S.fail_count, S.fence_errors = 0, 0
		S.epoch, S.generation, S.seq = body.epoch, body.generation, 0
		if type(body.notice_seconds) == "table" then
			S.ttl.done = (num(body.notice_seconds.done) or 15) * 1000 + 5000
			S.ttl.failed = (num(body.notice_seconds.failed) or 30) * 1000 + 5000
		end
		S.disabled = false
		recovered()
		S.dirty = true -- the activating snapshot is a separate call
	else
		S.fail_count = 0
		handle_error(str(body.error) or "helper_failed", body)
	end
	if cx and S.dirty then
		pump(cx)
	end
end

local function on_snapshot(body: any, code: string?, cx: WindowCx?)
	S.in_flight = false
	if body == nil then
		backoff(code or "helper_failed", nil)
	elseif body.ok == true then
		S.fail_count, S.fence_errors = 0, 0
		recovered()
		S.last_ack = str(body.desired)
	else
		S.fail_count = 0
		handle_error(str(body.error) or "helper_failed", body)
	end
	if cx and S.dirty then
		pump(cx)
	end
end

local function start_register(cx: WindowCx)
	S.in_flight = true
	run_helper("register", { observer = { window_key = S.window_key }, plugin_version = VERSION }, on_register)
end

local function start_snapshot(cx: WindowCx)
	local coverage, observations = enumerate(cx)
	S.seq += 1
	local payload = {
		observer = { window_key = S.window_key },
		epoch = S.epoch,
		generation = S.generation,
		seq = S.seq,
		coverage = coverage,
		observations = observations,
		diag = diag_report(),
	}
	S.in_flight = true
	run_helper("snapshot", payload, on_snapshot)
end

pump = function(cx: WindowCx)
	if S.inert or S.in_flight or not S.dirty then
		return
	end
	if is_disabled() then
		S.disabled = true
		S.dirty = false
		return
	end
	S.disabled = false
	local wait = S.retry_at - now_ms()
	if wait > 0 then
		if not S.flush_armed then
			S.flush_armed = true
			tern.timer(wait, function(cx2: WindowCx?)
				S.flush_armed = false
				S.dirty = true
				if cx2 then
					pump(cx2)
				end
			end)
		end
		return
	end
	S.dirty = false
	if S.generation == nil then
		start_register(cx)
	else
		start_snapshot(cx)
	end
end

local function arm_recon()
	if S.recon_armed then
		return
	end
	S.recon_armed = true
	tern.timer(RECON_MS, function(cx: WindowCx?)
		S.recon_armed = false
		arm_recon()
		S.dirty = true
		if cx then
			pump(cx)
		end
	end)
end

-- ---- events ----------------------------------------------------------------------------------

tern.on("agent_state", function(ev, _cx)
	local pane = num(ev.pane)
	if pane ~= nil then
		if ev.old == nil then
			S.omp[pane] = nil -- a new agent: classify again
		end
		if ev.new == "working" or ev.new == "waiting_input" then
			S.notices[pane] = nil
		end
	end
	request_flush()
end)

tern.on("agent_finished", function(ev, _cx)
	local pane = num(ev.pane)
	if pane ~= nil then
		if ev.result == "done" or ev.result == "failed" then
			S.notice_seq += 1
			S.notices[pane] = { id = tostring(S.notice_seq), kind = ev.result, at = now_ms() }
		else
			S.notices[pane] = nil
			S.omp[pane] = nil
		end
	end
	request_flush()
end)

tern.on("pane_closed", function(ev, _cx)
	local pane = num(ev.pane)
	if pane ~= nil then
		S.notices[pane] = nil
		S.omp[pane] = nil
	end
	request_flush()
end)

tern.on("session_ended", function(_ev, _cx)
	request_flush()
end)

tern.on("budget", function(ev, _cx)
	if ev.state == "paused" then
		tally("budget_pauses")
	else
		request_flush()
	end
end)

-- ---- commands --------------------------------------------------------------------------------

local function toast_result(cx: WindowCx?, title: string, body: any, code: string?)
	if not cx then
		return
	end
	if body == nil then
		cx:toast("error", title, code or "helper failed")
	elseif body.ok == true then
		cx:toast("success", title, str(body.desired) or str(body.summary) or "ok")
	else
		cx:toast("error", title, str(body.error) or "error")
	end
end

tern.command({
	id = "doctor",
	title = "SidePulse bridge: doctor",
	icon = "sparkle",
	run = function(cx)
		run_helper("doctor", { window_key = S.window_key, diag = diag_report() }, function(body, code, cx2)
			if not cx2 then
				return
			end
			if body == nil then
				cx2:toast("error", "SidePulse bridge doctor", code or "helper failed")
				return
			end
			local level = str(body.level) or "error"
			local tone = if level == "ok" then "success" elseif level == "warn" then "info" else "error"
			local sub = str(body.summary) or ""
			if type(body.report) == "string" then
				sub = sub .. " · " .. body.report
			end
			if S.inert then
				sub = "window: " .. S.inert .. " · " .. sub
			elseif S.python == nil then
				sub = "window: no python found · " .. sub
			end
			cx2:toast(tone, "SidePulse bridge doctor", sub)
		end)
	end,
})

tern.command({
	id = "resync",
	title = "SidePulse bridge: resync",
	icon = "sparkle",
	run = function(_cx)
		S.generation, S.epoch = nil, nil -- fresh registration and a fresh complete snapshot
		run_helper("resync", {}, function(body, code, cx2)
			toast_result(cx2, "SidePulse bridge resync", body, code)
			request_flush()
		end)
	end,
})

tern.command({
	id = "disable",
	title = "SidePulse bridge: disable",
	icon = "sparkle",
	run = function(_cx)
		S.generation, S.epoch = nil, nil
		run_helper("disable", {}, function(body, code, cx2)
			S.disabled = true
			toast_result(cx2, "SidePulse bridge disabled", body, code)
		end)
	end,
})

tern.command({
	id = "enable",
	title = "SidePulse bridge: enable",
	icon = "sparkle",
	run = function(_cx)
		S.generation, S.epoch = nil, nil
		run_helper("enable", {}, function(body, code, cx2)
			S.disabled = false
			toast_result(cx2, "SidePulse bridge enabled", body, code)
			request_flush()
		end)
	end,
})

-- ---- bootstrap -------------------------------------------------------------------------------

local function bootstrap(cx: WindowCx?)
	if S.booted or not cx then
		return
	end
	S.booted = true
	local missing = {}
	if tern.runtime.os == "ios" then
		table.insert(missing, "processes")
	end
	if not (cx.agents and cx.agents.list) then
		table.insert(missing, "cx.agents:list")
	end
	if not (cx.hosts and cx.hosts.list) then
		table.insert(missing, "cx.hosts:list")
	end
	if not (cx.session and cx.session.surface) then
		table.insert(missing, "cx.session:surface")
	end
	if not (tern.process and tern.process.run) then
		table.insert(missing, "tern.process.run")
	end
	if #missing > 0 then
		S.inert = "missing " .. table.concat(missing, ", ")
		tern.log.error("tern-sidepulse inert:", S.inert)
		return
	end
	local ok, key = pcall(function()
		return cx:window_key()
	end)
	S.window_key = if ok and type(key) == "string" then key else ""
	resolve_python()
	arm_recon()
	request_flush()
end

tern.timer(50, bootstrap) -- a reload runs the entry again but not window_start
tern.on("window_start", function(cx)
	bootstrap(cx)
end)

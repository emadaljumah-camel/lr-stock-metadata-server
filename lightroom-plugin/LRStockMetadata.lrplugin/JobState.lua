local LrFileUtils = import "LrFileUtils"
local LrPathUtils = import "LrPathUtils"
local json = require "json"

local JobState = {}

local function ensureDirectory(path)
    if not LrFileUtils.exists(path) then LrFileUtils.createAllDirectories(path) end
end

function JobState.defaultRoot()
    return LrPathUtils.child(LrPathUtils.getStandardFilePath("documents"), "LR Stock Metadata Jobs")
end

function JobState.activePath(jobRoot)
    ensureDirectory(jobRoot)
    return LrPathUtils.child(jobRoot, "active_job.json")
end

function JobState.createFolder(jobRoot, clientJobId)
    ensureDirectory(jobRoot)
    local folder = LrPathUtils.child(jobRoot, "job_" .. clientJobId)
    ensureDirectory(folder)
    ensureDirectory(LrPathUtils.child(folder, "previews"))
    return folder
end

function JobState.writeJson(path, value)
    local temporary, backup = path .. ".tmp", path .. ".bak"
    local file, openError = io.open(temporary, "w")
    if not file then return false, openError end
    file:write(json.encode(value)); file:write("\n"); file:close()
    if LrFileUtils.exists(backup) then LrFileUtils.delete(backup) end
    if LrFileUtils.exists(path) and not LrFileUtils.move(path, backup) then
        return false, "Could not back up local job state: " .. path
    end
    if not LrFileUtils.move(temporary, path) then
        if LrFileUtils.exists(backup) then LrFileUtils.move(backup, path) end
        return false, "Could not replace local job state: " .. path
    end
    if LrFileUtils.exists(backup) then LrFileUtils.delete(backup) end
    return true
end

function JobState.readJson(path)
    if not LrFileUtils.exists(path) and LrFileUtils.exists(path .. ".bak") then
        LrFileUtils.move(path .. ".bak", path)
    end
    local file, openError = io.open(path, "r")
    if not file then return nil, openError end
    local text = file:read("*all"); file:close()
    local ok, value = pcall(function() return json.decode(text) end)
    if not ok then return nil, tostring(value) end
    return value
end

function JobState.loadActive(jobRoot)
    local path = JobState.activePath(jobRoot)
    if not LrFileUtils.exists(path) then return nil end
    return JobState.readJson(path)
end

function JobState.saveActive(jobRoot, state)
    return JobState.writeJson(JobState.activePath(jobRoot), state)
end

return JobState

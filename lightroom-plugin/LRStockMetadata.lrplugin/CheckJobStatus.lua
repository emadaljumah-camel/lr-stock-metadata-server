local LrApplication = import "LrApplication"
local LrDialogs = import "LrDialogs"
local LrFileUtils = import "LrFileUtils"
local LrPathUtils = import "LrPathUtils"
local LrPrefs = import "LrPrefs"
local LrProgressScope = import "LrProgressScope"
local LrTasks = import "LrTasks"

local JobClient = require "JobClient"
local JobState = require "JobState"
local pluginConfig = require "PluginConfig"

local prefs = LrPrefs.prefsForPlugin()

local function repairManifestFilenames(manifest)
    local changed = false
    local serverById = {}
    for _, serverPhoto in ipairs(manifest.server_photos or {}) do
        serverById[serverPhoto.photo_id] = serverPhoto
    end
    for index, entry in ipairs(manifest.photos or {}) do
        local filename = tostring(entry.filename or "")
        if filename == "" and entry.original_path and entry.original_path ~= "" then
            filename = LrPathUtils.leafName(entry.original_path) or ""
        end
        if filename == "" then
            filename = "photo-" .. tostring(entry.local_identifier or index) .. ".jpg"
        end
        if entry.filename ~= filename then
            entry.filename = filename
            changed = true
        end
        local serverPhoto = serverById[entry.photo_id]
        if serverPhoto and tostring(serverPhoto.original_filename or "") == "" then
            serverPhoto.original_filename = filename
            changed = true
        end
    end
    return changed
end

local function resolvePhotos(catalog, manifest)
    local byId, wantedByLocalId = {}, {}
    for _, entry in ipairs(manifest.photos or {}) do
        if entry.catalog_uuid and entry.catalog_uuid ~= "" then
            local ok, photo = pcall(function() return catalog:findPhotoByUuid(entry.catalog_uuid) end)
            if ok and photo then byId[entry.photo_id] = photo end
        end
        if not byId[entry.photo_id] and entry.local_identifier and entry.local_identifier ~= "" then
            wantedByLocalId[entry.local_identifier] = entry.photo_id
        end
    end
    if next(wantedByLocalId) then
        for _, photo in ipairs(catalog:getAllPhotos()) do
            local photoId = wantedByLocalId[tostring(photo.localIdentifier or "")]
            if photoId and not byId[photoId] then byId[photoId] = photo end
        end
    end
    local missing = {}
    for _, entry in ipairs(manifest.photos or {}) do
        if not byId[entry.photo_id] then missing[#missing + 1] = entry.photo_id end
    end
    return byId, missing
end

local function validMetadata(item)
    local valid = type(item) == "table"
        and type(item.photo_id) == "string"
        and type(item.title) == "string"
        and type(item.description) == "string"
        and type(item.keywords) == "table"
        and type(item.agency_instruction) == "string"
        and type(item.editorial) == "boolean"
        and type(item.commercial) == "boolean"
        and type(item.reason) == "string"
    if not valid then return false end
    for _, keyword in ipairs(item.keywords) do
        if type(keyword) ~= "string" then return false end
    end
    return true
end

local function validateResult(result, active, manifest, photoById)
    if type(result) ~= "table" or result.status ~= "completed" or type(result.results) ~= "table" then
        return nil, "The server result is not a completed metadata job."
    end
    if result.job_id ~= active.job_id then return nil, "The server result belongs to a different job." end
    local expected, byId = {}, {}
    for _, entry in ipairs(manifest.photos or {}) do expected[entry.photo_id] = true end
    for _, item in ipairs(result.results) do
        if not validMetadata(item) then return nil, "The server returned invalid metadata." end
        if not expected[item.photo_id] then return nil, "The server returned an unknown photo ID: " .. item.photo_id end
        if byId[item.photo_id] then return nil, "The server returned a duplicate photo ID: " .. item.photo_id end
        if not photoById[item.photo_id] then return nil, "The original Lightroom photo is unavailable: " .. item.photo_id end
        byId[item.photo_id] = item
    end
    for photoId, _ in pairs(expected) do
        if not byId[photoId] then return nil, "The server result is missing photo ID: " .. photoId end
    end
    return byId
end

local function applyResults(catalog, manifest, resultById, photoById, progress)
    local writeStatus = catalog:withWriteAccessDo(
        "Apply Stock Metadata Job",
        function()
            for index, entry in ipairs(manifest.photos) do
                local metadata, photo = resultById[entry.photo_id], photoById[entry.photo_id]
                progress:setCaption("Applying " .. tostring(index) .. " of " .. tostring(#manifest.photos) .. ": " .. entry.filename)
                progress:setPortionComplete(index - 1, #manifest.photos)
                photo:setRawMetadata("title", metadata.title)
                photo:setRawMetadata("caption", metadata.description)
                for _, keywordName in ipairs(metadata.keywords) do
                    local keyword = catalog:createKeyword(keywordName, {}, true, nil, true)
                    photo:addKeyword(keyword)
                end
                photo:setRawMetadata("instructions", metadata.agency_instruction)
            end
        end,
        { timeout = 30 }
    )
    if writeStatus ~= nil and writeStatus ~= "executed" then
        return nil, "Lightroom catalog write access timed out."
    end
    return { updated = #manifest.photos }
end

LrTasks.startAsyncTask(function()
    local jobRoot = prefs.jobRoot or JobState.defaultRoot()
    local active, activeError = JobState.loadActive(jobRoot)
    if not active then
        LrDialogs.message("Stock Metadata", activeError or "There is no active metadata job.", activeError and "critical" or "info")
        return
    end
    if active.applied then
        LrDialogs.message("Stock Metadata", "Job " .. tostring(active.job_id) .. " has already been applied.", "info")
        return
    end

    local manifestPath = LrPathUtils.child(active.job_folder, "job.json")
    local manifest, manifestError = JobState.readJson(manifestPath)
    if not manifest then
        LrDialogs.message("Stock metadata job error", manifestError, "critical")
        return
    end
    if repairManifestFilenames(manifest) then
        local repaired, repairError = JobState.writeJson(manifestPath, manifest)
        if not repaired then
            LrDialogs.message("Stock metadata job error", repairError, "critical")
            return
        end
    end
    local settings = {
        serverUrl = active.endpoint_url or prefs.serverUrl or pluginConfig.serverUrl,
        timeoutSeconds = 120,
    }
    local progress = LrProgressScope { title = "Check Stock Metadata Job" }
    progress:setCancelable(true)
    local resultPath = LrPathUtils.child(active.job_folder, "completed_result.json")
    local result

    if active.status == "downloaded" and LrFileUtils.exists(resultPath) then
        result, manifestError = JobState.readJson(resultPath)
        if not result then progress:done(); LrDialogs.message("Local result error", manifestError, "critical"); return end
    else
        if active.status == "submitting" then
            local submitted, submitError = JobClient.submit(settings, manifest, progress)
            if not submitted then
                progress:done()
                LrDialogs.message("Stock metadata submission interrupted", submitError .. "\n\nThe saved job can be resumed later.", "warning")
                return
            end
            active.job_id = submitted.job_id or active.job_id
            active.status = submitted.status or "running"
            JobState.saveActive(jobRoot, active)
        end

        progress:setCaption("Checking server status...")
        local status, statusError = JobClient.status(settings, active.job_id)
        if not status then progress:done(); LrDialogs.message("Stock metadata status error", statusError, "critical"); return end
        active.status = status.status
        JobState.saveActive(jobRoot, active)
        if status.status == "created" or status.status == "running" then
            progress:done()
            local jobProgress = status.progress or {}
            LrDialogs.message(
                "Stock metadata job still running",
                "Stage: " .. tostring(jobProgress.stage or "unknown") .. "\nProgress: "
                    .. tostring(jobProgress.processed_photos or 0) .. "/"
                    .. tostring(jobProgress.total_photos or #manifest.photos),
                "info"
            )
            return
        end
        if status.status == "failed" or status.status == "interrupted" then
            local errors = table.concat(status.errors or {}, "\n")
            local resume = LrDialogs.confirm(
                "Stock metadata job interrupted",
                errors ~= "" and errors or "The job can be resumed from its checkpoint.",
                "Resume Job",
                "Cancel"
            )
            if resume == "ok" then
                local resumed, resumeError = JobClient.run(settings, active.job_id)
                if not resumed then progress:done(); LrDialogs.message("Resume error", resumeError, "critical"); return end
                active.status = resumed.status or "running"
                JobState.saveActive(jobRoot, active)
                progress:done(); LrDialogs.message("Stock metadata job resumed", "The server resumed job " .. active.job_id .. ".", "info"); return
            end
            progress:done(); return
        end
        if status.status ~= "completed" then
            progress:done(); LrDialogs.message("Stock Metadata", "Unknown server job status: " .. tostring(status.status), "warning"); return
        end

        progress:setCaption("Downloading completed results...")
        local downloadError
        result, downloadError = JobClient.result(settings, active.job_id)
        if not result then progress:done(); LrDialogs.message("Result download error", downloadError, "critical"); return end
        local resultSaved, resultSaveError = JobState.writeJson(resultPath, result)
        if not resultSaved then progress:done(); LrDialogs.message("Local result error", resultSaveError, "critical"); return end
        active.status = "downloaded"
        JobState.saveActive(jobRoot, active)
    end

    local catalog = LrApplication.activeCatalog()
    if active.catalog_path ~= "" and tostring(catalog:getPath() or "") ~= active.catalog_path then
        progress:done()
        LrDialogs.message("Stock metadata catalog mismatch", "Open the Lightroom catalog that submitted this job before applying it.", "critical")
        return
    end
    local photoById, missing = resolvePhotos(catalog, manifest)
    if #missing > 0 then
        progress:done()
        LrDialogs.message("Stock metadata missing photos", "Could not find " .. tostring(#missing) .. " original photo(s). No metadata was changed.\n\n" .. table.concat(missing, "\n"), "critical")
        return
    end
    local resultById, validationError = validateResult(result, active, manifest, photoById)
    if not resultById then
        progress:done(); LrDialogs.message("Invalid stock metadata result", validationError .. "\nNo metadata was changed.", "critical"); return
    end

    local ok, summaryOrError, applyError = LrTasks.pcall(function()
        return applyResults(catalog, manifest, resultById, photoById, progress)
    end)
    progress:done()
    if not ok or not summaryOrError then
        LrDialogs.message("Stock metadata apply error", tostring(applyError or summaryOrError) .. "\nThe downloaded result remains available for retry.", "critical")
        return
    end
    active.applied = true
    active.status = "applied"
    active.applied_at = os.time()
    JobState.saveActive(jobRoot, active)
    LrDialogs.message("Stock metadata job applied", "Job: " .. active.job_id .. "\nUpdated: " .. tostring(summaryOrError.updated), "info")
end)

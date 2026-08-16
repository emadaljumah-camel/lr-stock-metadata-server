local LrApplication = import "LrApplication"
local LrDate = import "LrDate"
local LrDialogs = import "LrDialogs"
local LrTasks = import "LrTasks"
local LrExportSession = import "LrExportSession"
local LrPathUtils = import "LrPathUtils"
local LrProgressScope = import "LrProgressScope"
local LrPrefs = import "LrPrefs"
local pluginConfig = require "PluginConfig"
local JobClient = require "JobClient"
local JobState = require "JobState"

local prefs = LrPrefs.prefsForPlugin()
local DEFAULT_SERVER_URL = pluginConfig.serverUrl or "http://127.0.0.1:8008"

local function getMeta(photo, key)
    local ok, value = pcall(function() return photo:getRawMetadata(key) end)
    if ok and value ~= nil then return tostring(value) end
    return ""
end

local function getFormattedMeta(photo, key)
    local ok, value = pcall(function() return photo:getFormattedMetadata(key) end)
    if ok and value ~= nil then return tostring(value) end
    return ""
end

local function photoFilename(photo, index)
    local filename = getMeta(photo, "fileName")
    if filename == "" then filename = getFormattedMeta(photo, "fileName") end
    if filename == "" then
        local originalPath = getMeta(photo, "path")
        if originalPath ~= "" then filename = LrPathUtils.leafName(originalPath) or "" end
    end
    if filename == "" then
        filename = "photo-" .. tostring(photo.localIdentifier or index) .. ".jpg"
    end
    return filename
end

local function hasExistingMetadata(photo)
    return getMeta(photo, "title") ~= "" or getMeta(photo, "caption") ~= "" or #(photo:getRawMetadata("keywords") or {}) > 0
end

local function exportPreview(photo, folder)
    local settings = {
        LR_export_destinationType = "specificFolder",
        LR_export_destinationPathPrefix = folder,
        LR_format = "JPEG",
        LR_jpeg_quality = 0.80,
        LR_size_doConstrain = true,
        LR_size_maxHeight = 1600,
        LR_size_maxWidth = 1600,
        LR_size_resizeType = "wh",
        LR_collisionHandling = "rename",
        LR_minimizeEmbeddedMetadata = false,
        LR_removeLocationMetadata = false,
        LR_embeddedMetadataOption = "all",
    }
    local session = LrExportSession { photosToExport = { photo }, exportSettings = settings }
    for _, rendition in session:renditions { stopIfCanceled = true } do
        local success, pathOrMessage = rendition:waitForRender()
        if success then return pathOrMessage end
        return nil, pathOrMessage
    end
    return nil, "No rendition created"
end

local function buildContext(photo)
    return {
        capture_time = getMeta(photo, "dateTimeOriginal"),
        camera_model = getMeta(photo, "cameraModel"),
        lens = getMeta(photo, "lens"),
        iso = getMeta(photo, "isoSpeedRating"),
        country = getMeta(photo, "country"),
        city = getMeta(photo, "city"),
        state = getMeta(photo, "stateProvince"),
        sublocation = getMeta(photo, "location"),
    }
end

LrTasks.startAsyncTask(function()
    math.randomseed(os.time())
    local catalog = LrApplication.activeCatalog()
    local photos = catalog:getTargetPhotos()
    if #photos == 0 then
        LrDialogs.message("Generate Stock Metadata", "Select one or more photos first.", "info")
        return
    end

    local settings = {
        serverUrl = prefs.serverUrl or DEFAULT_SERVER_URL,
        jobRoot = prefs.jobRoot or JobState.defaultRoot(),
        timeoutSeconds = 120,
    }
    local active, activeError = JobState.loadActive(settings.jobRoot)
    if activeError then
        LrDialogs.message("Stock metadata job error", activeError, "critical")
        return
    end
    if active and not active.applied and not active.closed then
        LrDialogs.message(
            "Stock metadata job pending",
            "Job " .. tostring(active.job_id)
                .. " has not been applied. Use 'Check Job Status / Apply Results' first.",
            "warning"
        )
        return
    end

    local overwrite = LrDialogs.confirm(
        "Overwrite existing metadata?",
        "Overwrite existing title, caption, and keywords when present?",
        "Overwrite",
        "Skip Existing"
    ) == "ok"

    local clientJobId = os.date("%Y%m%d-%H%M%S") .. "-"
        .. tostring(math.random(100000, 999999))
    local jobFolder = JobState.createFolder(settings.jobRoot, clientJobId)
    local previewDir = LrPathUtils.child(jobFolder, "previews")
    local progress = LrProgressScope { title = "Generating stock metadata" }
    progress:setCancelable(true)
    local manifest = {
        schema_version = 1,
        client_job_id = clientJobId,
        plugin_version = "1.1.0",
        catalog_path = tostring(catalog:getPath() or ""),
        endpoint_url = settings.serverUrl,
        overwrite = overwrite,
        photos = {},
        server_photos = {},
    }
    local skipped = 0
    for index, photo in ipairs(photos) do
        if progress:isCanceled() then break end
        progress:setPortionComplete(index - 1, #photos)
        if (not overwrite) and hasExistingMetadata(photo) then
            skipped = skipped + 1
        else
            local filename = photoFilename(photo, index)
            progress:setCaption("Exporting " .. filename)
            local previewPath, exportErr = exportPreview(photo, previewDir)
            if not previewPath then
                progress:done()
                LrDialogs.message("Export failed", exportErr or "Could not export preview.", "critical")
                return
            end
            local uuid = getMeta(photo, "uuid")
            local photoId = uuid ~= "" and uuid or ("photo-" .. tostring(photo.localIdentifier or index))
            manifest.photos[#manifest.photos + 1] = {
                photo_id = photoId,
                catalog_uuid = uuid,
                local_identifier = tostring(photo.localIdentifier or ""),
                original_path = getMeta(photo, "path"),
                filename = filename,
                preview_path = previewPath,
            }
            manifest.server_photos[#manifest.server_photos + 1] = {
                photo_id = photoId,
                original_filename = filename,
                context = buildContext(photo),
            }
        end
    end

    if progress:isCanceled() then progress:done(); return end
    if #manifest.photos == 0 then
        progress:done()
        LrDialogs.message("Generate Stock Metadata", "No photos were queued; all selected photos already contain metadata.", "info")
        return
    end
    local saved, saveError = JobState.writeJson(LrPathUtils.child(jobFolder, "job.json"), manifest)
    if not saved then
        progress:done(); LrDialogs.message("Stock metadata job error", saveError, "critical"); return
    end
    local activeState = {
        schema_version = 1,
        job_id = clientJobId,
        client_job_id = clientJobId,
        job_folder = jobFolder,
        catalog_path = manifest.catalog_path,
        endpoint_url = settings.serverUrl,
        status = "submitting",
        applied = false,
        closed = false,
        submitted_at = os.time(),
    }
    local activeSaved, activeSaveError = JobState.saveActive(settings.jobRoot, activeState)
    if not activeSaved then
        progress:done(); LrDialogs.message("Stock metadata job error", activeSaveError, "critical"); return
    end
    local response, submitError = JobClient.submit(settings, manifest, progress)
    if not response then
        progress:done()
        LrDialogs.message(
            "Stock metadata submission interrupted",
            submitError .. "\n\nThe local job is saved. Use 'Check Job Status / Apply Results' to resume.",
            "warning"
        )
        return
    end
    activeState.job_id = response.job_id or clientJobId
    activeState.status = response.status or "running"
    JobState.saveActive(settings.jobRoot, activeState)
    progress:setPortionComplete(100, 100); progress:done()
    LrDialogs.message(
        "Stock metadata job submitted",
        "Job " .. activeState.job_id .. " is processing on the server.\n\n"
            .. "It is safe to disconnect. Reconnect later and use "
            .. "'Check Job Status / Apply Results'.\nSkipped existing: " .. tostring(skipped),
        "info"
    )
end)

-- AI Rotoscope Script for DaVinci Resolve
-- Can be run from Workspace -> Scripts -> Edit / Comp / Utility

local function GetResolveInstance()
    if app and app.GetResolve then
        local r = app:GetResolve()
        if r then return r end
    end
    if fusion and fusion.GetResolve then
        local r = fusion:GetResolve()
        if r then return r end
    end
    if bmd and bmd.scriptapp then
        local r = bmd.scriptapp("Resolve")
        if r then return r end
    end
    if Resolve then
        local ok, r = pcall(Resolve)
        if ok and r then return r end
    end
    return nil
end

local function GetSelectedClipPath()
    local res = GetResolveInstance()
    if res then
        local pm = res:GetProjectManager()
        if pm then
            local proj = pm:GetCurrentProject()
            if proj then
                local tl = proj:GetCurrentTimeline()
                if tl then
                    local item = tl:GetCurrentVideoItem()
                    if item then
                        local mpi = item:GetMediaPoolItem()
                        if mpi then
                            local fp = mpi:GetClipProperty("File Path")
                            if fp and fp ~= "" then
                                return fp
                            end
                        end
                    end
                end
            end
        end
    end

    -- If in Fusion comp, check MediaIn nodes
    if fusion then
        local comp = fusion:GetCurrentComp()
        if comp then
            local mediaIns = comp:GetToolList(false, "MediaIn") or {}
            for _, mIn in ipairs(mediaIns) do
                local out = mIn.Output
                if out then
                    local img = out:GetValue(comp.CurrentTime or 0)
                    if img and img.Metadata and img.Metadata.Filename then
                        local fn = img.Metadata.Filename
                        if fn and fn ~= "" then return fn end
                    end
                end
            end
        end
    end

    return ""
end

local clip = GetSelectedClipPath()
local cmd
if clip and clip ~= "" then
    cmd = 'start "" "E:\\Apps\\MyRotoTool\\run_roto.bat" "' .. clip .. '"'
else
    cmd = 'start "" "E:\\Apps\\MyRotoTool\\run_roto.bat"'
end
os.execute(cmd)

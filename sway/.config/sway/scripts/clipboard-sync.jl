#!/usr/bin/julia
using Dates
import Base.read
import Base.:<
import Base.:>
import Base.isapprox

struct Clipboard
    name :: String
    read :: Cmd
    write :: Cmd
end

struct ClipRecord
    content :: Union{String, Nothing}
    time :: DateTime
end
>(x :: ClipRecord, y :: ClipRecord) = x.time > y.time
<(x :: ClipRecord, y :: ClipRecord) = x.time < y.time

function isapprox(x :: ClipRecord, y :: ClipRecord)
    if x.content === nothing || y.content === nothing return false end
    return occursin(x.content, y.content) || occursin(y.content, x.content)
end

ClipRecord() = ClipRecord(nothing, typemin(DateTime))

xclip_clipboard = Clipboard("xclip_clipboard",
                            `xclip -selection clipboard -o`,
                           `xclip -selection clipboard`)
xclip_primary = Clipboard("xlclip_primary",
                          `xclip -o`,
                         `xclip`)
wl_clipboard = Clipboard("wl_clipboard",
                         `wl-paste`,
                        `wl-copy`)
wl_primary = Clipboard("wl_primary",
                       `wl-paste -p`,
                            `wl-copy -p`)

function read(cb :: Clipboard) :: ClipRecord
    try
        s = read(pipeline(cb.read, stderr=devnull), String)
        clip = ClipRecord(s, now())
        return clip
    catch e
        return ClipRecord(nothing, now())
    end
end

function write(cb :: Clipboard, content)
    try
        s = run(cb.write, IOBuffer(content))
    catch e
        error(e)
    end
end


# clipboard_set = (xclip_clipboard, xclip_primary, wl_clipboard, wl_primary)
clipboard_set = (xclip_clipboard, wl_clipboard)
clipboards = map(clipboard_set) do cb
    (cb, ClipRecord())
end |> Dict

record_master = ClipRecord()
while true
    # refresh contents
    for (clipboard, record) in clipboards
        new_record = read(clipboard)
        if new_record.content != record.content
            clipboards[clipboard] = new_record
        end
    end

    # check if any entries are newer than record_master
    # latest entry wins
    for (clipboard, record) in clipboards
        if record.content !== nothing &&
                !isapprox(record, record_master) &&
                record.time > record_master.time
            println("$(clipboard.name) => master")
            global record_master = record
        end
    end

    # write to any outdated clipboards
    for (clipboard, record) in clipboards
            if record_master.content !== nothing &&
                !isapprox(record, record_master)
            println("master => $(clipboard.name)")
            write(clipboard, record_master.content)
        end
    end
    sleep(0.3)
end

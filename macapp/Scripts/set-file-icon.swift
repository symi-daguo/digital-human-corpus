#!/usr/bin/env swift
//
// 给文件本身贴自定义图标（不是卷图标）。
//
//   swift Scripts/set-file-icon.swift <icns|png> <目标文件>
//
// 为什么需要这个：hdiutil 打出来的 .dmg 在 Finder 里默认是通用磁盘图标。
// 挂载后卷图标靠 .VolumeIcon.icns 生效，但**未挂载的 dmg 文件**要显示我们
// 的图标，必须把图标写进文件的 resource fork，这一步没有现成的 CLI 能做。
//
import AppKit
import Foundation

let args = CommandLine.arguments
guard args.count == 3 else {
    FileHandle.standardError.write("用法: set-file-icon <icns|png> <目标文件>\n".data(using: .utf8)!)
    exit(2)
}

let iconPath = args[1]
let target = args[2]

guard let img = NSImage(contentsOfFile: iconPath) else {
    FileHandle.standardError.write("读图失败: \(iconPath)\n".data(using: .utf8)!)
    exit(1)
}

guard NSWorkspace.shared.setIcon(img, forFile: target, options: []) else {
    FileHandle.standardError.write("设置图标失败: \(target)\n".data(using: .utf8)!)
    exit(1)
}

print("OK 已设置图标: \(target)")

// swift-tools-version:6.0
import PackageDescription

let package = Package(
    name: "DigitalHuman",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "DigitalHumanApp",
            path: "Sources/DigitalHumanApp"
        )
    ]
)

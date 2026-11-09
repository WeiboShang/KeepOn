// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "KeepOnComputerHelper",
    platforms: [.macOS(.v13)],
    products: [
        .library(name: "KeepOnComputerCore", targets: ["KeepOnComputerCore"]),
        .executable(name: "keepon-computer-helper", targets: ["KeepOnComputerHelper"]),
    ],
    targets: [
        .target(name: "KeepOnComputerCore"),
        .executableTarget(name: "KeepOnComputerHelper", dependencies: ["KeepOnComputerCore"]),
        .testTarget(name: "KeepOnComputerCoreTests", dependencies: ["KeepOnComputerCore"]),
    ]
)

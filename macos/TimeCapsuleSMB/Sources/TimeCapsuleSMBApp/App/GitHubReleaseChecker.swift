import Foundation

struct AppReleaseVersion: Equatable {
    let components: [Int]
    let isPrerelease: Bool

    init?(_ value: String) {
        let pattern = #"^v?[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?(?:\+[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?$"#
        guard value.range(of: pattern, options: .regularExpression) == value.startIndex..<value.endIndex else { return nil }
        let version = value.hasPrefix("v") ? String(value.dropFirst()) : value
        let withoutBuild = version.split(separator: "+", maxSplits: 1)[0]
        let parts = withoutBuild.split(separator: "-", maxSplits: 1)
        let components = parts[0].split(separator: ".").compactMap { Int($0) }
        guard components.count == 3 else { return nil }
        self.components = components
        isPrerelease = parts.count > 1
    }

    func isNewerStableRelease(than installed: AppReleaseVersion) -> Bool {
        guard !isPrerelease else { return false }
        if components == installed.components {
            return installed.isPrerelease
        }
        return installed.components.lexicographicallyPrecedes(components)
    }
}

struct GitHubAppRelease: Equatable {
    let tag: String
    let downloadURL: URL
}

enum AppUpdateError: LocalizedError {
    case installedVersionUnavailable
    case invalidRelease
    case missingPackage
    case httpStatus(Int)
    case openDownloadFailed

    var errorDescription: String? {
        switch self {
        case .installedVersionUnavailable:
            return L10n.string("app_update.error.installed_version")
        case .invalidRelease:
            return L10n.string("app_update.error.invalid_release")
        case .missingPackage:
            return L10n.string("app_update.error.missing_package")
        case .httpStatus(let status):
            return L10n.format("app_update.error.http_status", status)
        case .openDownloadFailed:
            return L10n.string("app_update.error.open_download")
        }
    }
}

protocol AppReleaseChecking {
    func latestRelease() async throws -> GitHubAppRelease
}

struct GitHubReleaseChecker: AppReleaseChecking {
    static let feedURL = URL(string: "https://api.github.com/repos/jamesyc/TimeCapsuleSMB/releases/latest")!
    var session: URLSession = .shared

    func latestRelease() async throws -> GitHubAppRelease {
        var request = URLRequest(url: Self.feedURL, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 20)
        request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        request.setValue("2022-11-28", forHTTPHeaderField: "X-GitHub-Api-Version")
        request.setValue("TimeCapsuleSMB", forHTTPHeaderField: "User-Agent")
        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse else {
            throw AppUpdateError.invalidRelease
        }
        guard response.statusCode == 200 else {
            throw AppUpdateError.httpStatus(response.statusCode)
        }
        let release = try JSONDecoder().decode(Release.self, from: data)
        guard !release.draft, !release.prerelease,
              let version = AppReleaseVersion(release.tag_name), !version.isPrerelease else {
            throw AppUpdateError.invalidRelease
        }
        guard let asset = release.assets.first(where: { $0.name == "TimeCapsuleSMB.app.zip" }),
              let url = URL(string: asset.browser_download_url),
              url.scheme == "https", url.host == "github.com",
              url.user == nil, url.password == nil, url.port == nil,
              url.query == nil, url.fragment == nil,
              url.path == "/jamesyc/TimeCapsuleSMB/releases/download/\(release.tag_name)/TimeCapsuleSMB.app.zip" else {
            throw AppUpdateError.missingPackage
        }
        return GitHubAppRelease(tag: release.tag_name, downloadURL: url)
    }

    private struct Release: Decodable {
        let tag_name: String
        let draft: Bool
        let prerelease: Bool
        let assets: [Asset]
    }

    private struct Asset: Decodable {
        let name: String
        let browser_download_url: String
    }
}

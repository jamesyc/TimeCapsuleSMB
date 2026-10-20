import XCTest
@testable import TimeCapsuleSMBApp

final class GitHubReleaseCheckerTests: XCTestCase {
    func testVersionComparisonIsNumericAndNeverDowngrades() throws {
        for (installed, latest, expected) in [
            ("3.6.0", "v3.7.0", true),
            ("3.9.0", "v3.10.0", true),
            ("3.6.9", "v3.6.10", true),
            ("3.6.0", "v4.0.0", true),
            ("3.6.0", "v3.6.0", false),
            ("3.7.0", "v3.6.0", false),
            ("3.7.0-beta.1", "v3.7.0", true),
            ("3.8.0-beta.1", "v3.7.0", false),
            ("3.7.0+build.42", "v3.7.0", false),
            ("3.6.0", "v3.7.0-rc.1", false)
        ] {
            let local = try XCTUnwrap(AppReleaseVersion(installed))
            let remote = try XCTUnwrap(AppReleaseVersion(latest))
            XCTAssertEqual(remote.isNewerStableRelease(than: local), expected, "\(installed) -> \(latest)")
        }
        for invalid in ["", "latest", "v3", "3.6", "3.6.x", "3.6.0.1", "3.6.0-", "3.6.0\n"] {
            XCTAssertNil(AppReleaseVersion(invalid), invalid)
        }
    }

    func testRequestsGitHubLatestStableReleaseAndFindsAppPackage() async throws {
        let (checker, session) = makeChecker()
        defer { session.invalidateAndCancel() }

        let release = try await checker.latestRelease()

        XCTAssertEqual(release.tag, "v3.7.0")
        XCTAssertEqual(release.downloadURL.absoluteString,
                       "https://github.com/jamesyc/TimeCapsuleSMB/releases/download/v3.7.0/TimeCapsuleSMB.app.zip")
    }

    func testRejectsHTTPFailuresIncludingRateLimitAndNoReleases() async throws {
        for status in [403, 404, 429, 500] {
            let (checker, session) = makeChecker(status: status)
            defer { session.invalidateAndCancel() }
            do {
                _ = try await checker.latestRelease()
                XCTFail("Expected HTTP \(status) failure")
            } catch AppUpdateError.httpStatus(let actual) {
                XCTAssertEqual(actual, status)
            }
        }
    }

    func testRejectsDraftPrereleaseMalformedAndMissingPackageResponses() async throws {
        for body in [
            releaseJSON(draft: true),
            releaseJSON(prerelease: true),
            releaseJSON(tag: "v3.7.0-rc.1"),
            releaseJSON(tag: "latest"),
            releaseJSON(assetName: "source.zip"),
            releaseJSON(downloadURL: "https://example.invalid/TimeCapsuleSMB.app.zip"),
            releaseJSON(downloadURL: "http://github.com/jamesyc/TimeCapsuleSMB/releases/download/v3.7.0/TimeCapsuleSMB.app.zip"),
            releaseJSON(downloadURL: "https://github.com/other/repo/releases/download/v3.7.0/TimeCapsuleSMB.app.zip"),
            releaseJSON(downloadURL: "https://github.com/jamesyc/TimeCapsuleSMB/releases/download/v3.6.0/TimeCapsuleSMB.app.zip"),
            "{}",
            "<html>Service unavailable</html>"
        ] {
            let (checker, session) = makeChecker(body: body)
            defer { session.invalidateAndCancel() }
            do {
                _ = try await checker.latestRelease()
                XCTFail("Expected invalid release failure: \(body)")
            } catch {
                XCTAssertFalse(error.localizedDescription.isEmpty)
            }
        }
    }

    private func makeChecker(status: Int = 200, body: String? = nil) -> (GitHubReleaseChecker, URLSession) {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ReleaseURLProtocol.self]
        configuration.httpAdditionalHeaders = [
            "Test-Status": String(status),
            "Test-Body": (body ?? releaseJSON()).data(using: .utf8)!.base64EncodedString()
        ]
        let session = URLSession(configuration: configuration)
        return (GitHubReleaseChecker(session: session), session)
    }

    private func releaseJSON(
        tag: String = "v3.7.0",
        draft: Bool = false,
        prerelease: Bool = false,
        assetName: String = "TimeCapsuleSMB.app.zip",
        downloadURL: String = "https://github.com/jamesyc/TimeCapsuleSMB/releases/download/v3.7.0/TimeCapsuleSMB.app.zip"
    ) -> String {
        """
        {"tag_name":"\(tag)","draft":\(draft),"prerelease":\(prerelease),"assets":[
            {"name":"source.zip","browser_download_url":"https://example.invalid/source.zip"},
            {"name":"\(assetName)","browser_download_url":"\(downloadURL)"}
        ]}
        """
    }
}

private final class ReleaseURLProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        XCTAssertEqual(request.url, GitHubReleaseChecker.feedURL)
        XCTAssertEqual(request.value(forHTTPHeaderField: "Accept"), "application/vnd.github+json")
        XCTAssertEqual(request.value(forHTTPHeaderField: "X-GitHub-Api-Version"), "2022-11-28")
        XCTAssertEqual(request.value(forHTTPHeaderField: "User-Agent"), "TimeCapsuleSMB")
        XCTAssertEqual(request.cachePolicy, .reloadIgnoringLocalCacheData)
        XCTAssertEqual(request.timeoutInterval, 20)
        let status = Int(request.value(forHTTPHeaderField: "Test-Status")!)!
        let data = Data(base64Encoded: request.value(forHTTPHeaderField: "Test-Body")!)!
        let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

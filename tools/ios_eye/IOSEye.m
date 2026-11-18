//
// IOSEye.m — native iPhone screen capture helper (no Xcode / no Developer Mode).
//
// Why this exists: on macOS an iPhone's screen is *not* an ordinary AVFoundation
// camera. It is exposed by the CoreMediaIO DAL plug-in
// `com.apple.cmio.DAL.iOSScreenCapture` (the same channel QuickTime Player uses
// for "New Movie Recording" -> iPhone). A process only sees the device after it
// has initialised the CoreMediaIO stack with screen-capture devices allowed;
// ffmpeg's avfoundation backend does not do that, so we drive AVFoundation
// ourselves and stream JPEG frames to Python over a unix socket.
//
// Modes:
//   --probe  --status-file PATH
//   --shot   --out PATH --status-file PATH
//   --stream --socket PATH [--max-width N] [--max-fps N] [--quality Q]
//             --status-file PATH
//
// All modes write a JSON status file (atomic) so the Python side can report
// precise errors (camera permission denied, iPhone not connected, system
// capture service missing, ...).
//
#import <Foundation/Foundation.h>
#import <dispatch/dispatch.h>
#import <AVFoundation/AVFoundation.h>
#import <AppKit/AppKit.h>
#import <CoreMediaIO/CoreMediaIO.h>
#import <CoreMedia/CoreMedia.h>
#import <CoreVideo/CoreVideo.h>
#import <ImageIO/ImageIO.h>

#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/uio.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>

static NSString *gStatusPath = nil;
static volatile sig_atomic_t gStop = 0;
@interface IOSEyeClient : NSObject
@property (nonatomic) int fd;
@property (nonatomic) BOOL closed;
@property (nonatomic, strong) NSMutableArray<NSData *> *queue;
@property (nonatomic, strong) NSCondition *condition;
@property (nonatomic, strong) NSThread *writer;
- (void)enqueue:(NSData *)packet;
- (void)shutdown;
@end

@implementation IOSEyeClient
- (instancetype)initWithFd:(int)fd {
    self = [super init];
    if (self) {
        _fd = fd;
        _closed = NO;
        _queue = [NSMutableArray array];
        _condition = [NSCondition new];
        _writer = [[NSThread alloc] initWithTarget:self selector:@selector(writeLoop) object:nil];
        [_writer start];
    }
    return self;
}

- (void)enqueue:(NSData *)packet {
    [self.condition lock];
    if (self.closed) {
        [self.condition unlock];
        return;
    }
    // Keep the client fresh: drop the oldest frame when it falls behind.
    while (self.queue.count >= 2) [self.queue removeObjectAtIndex:0];
    [self.queue addObject:packet];
    [self.condition signal];
    [self.condition unlock];
}

- (void)writeLoop {
    while (YES) {
        [self.condition lock];
        while (self.queue.count == 0 && !self.closed) {
            [self.condition waitUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.5]];
        }
        if (self.closed) {
            [self.condition unlock];
            return;
        }
        NSData *packet = self.queue.firstObject;
        [self.queue removeObjectAtIndex:0];
        [self.condition unlock];
        if (packet == nil) continue;
        const uint8_t *bytes = packet.bytes;
        NSUInteger total = packet.length;
        NSUInteger offset = 0;
        while (offset < total) {
            ssize_t sent = send(self.fd, bytes + offset, total - offset, 0);
            if (sent <= 0) {
                [self shutdown];
                return;
            }
            offset += (NSUInteger)sent;
        }
    }
}

- (void)shutdown {
    [self.condition lock];
    if (self.closed) {
        [self.condition unlock];
        return;
    }
    self.closed = YES;
    [self.queue removeAllObjects];
    [self.condition broadcast];
    [self.condition unlock];
    close(self.fd);
}
@end

static NSMutableArray<IOSEyeClient *> *gClients = nil;
static NSLock *gClientsLock = nil;
static NSString *gError = nil;
static NSInteger gFrames = 0;
static double gStartedAt = 0;
static double gLastFrameAt = 0;
static int gNativeWidth = 0;
static int gNativeHeight = 0;
static NSString *gDeviceName = nil;
static NSString *gDeviceModel = nil;
static NSString *gDeviceUid = nil;
static const char *kHelperVersion = "1.0.0";

static void write_status(NSString *mode, BOOL ok) {
    if (gStatusPath == nil) return;
    NSMutableDictionary *status = [NSMutableDictionary dictionary];
    status[@"ok"] = @(ok);
    status[@"mode"] = mode;
    status[@"pid"] = @(getpid());
    status[@"ts"] = @([[NSDate date] timeIntervalSince1970]);
    status[@"helper"] = @"IOSEye";
    status[@"helper_version"] = @(kHelperVersion);
    status[@"device"] = gDeviceName ?: [NSNull null];
    status[@"device_model"] = gDeviceModel ?: [NSNull null];
    status[@"device_uid"] = gDeviceUid ?: [NSNull null];
    status[@"width"] = @(gNativeWidth);
    status[@"height"] = @(gNativeHeight);
    status[@"frames"] = @(gFrames);
    status[@"fps"] = gStartedAt > 0 ? @(gFrames / MAX(0.001, [[NSDate date] timeIntervalSince1970] - gStartedAt)) : @0;
    status[@"age_ms"] = gLastFrameAt > 0 ? @((([[NSDate date] timeIntervalSince1970] - gLastFrameAt) * 1000.0)) : [NSNull null];
    status[@"error"] = gError ?: [NSNull null];
    status[@"camera_auth"] = @((long)[AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeVideo]);
    NSData *data = [NSJSONSerialization dataWithJSONObject:status options:0 error:NULL];
    if (data != nil) {
        [data writeToFile:gStatusPath atomically:YES];
    }
}

static void set_error(NSString *message) {
    gError = [message copy];
}

static void handle_signal(int sig) {
    (void)sig;
    gStop = 1;
}

static void close_clients(void) {
    [gClientsLock lock];
    NSArray<IOSEyeClient *> *clients = [gClients copy];
    [gClients removeAllObjects];
    [gClientsLock unlock];
    for (IOSEyeClient *client in clients) {
        [client shutdown];
    }
}

static int open_unix_server(NSString *path) {
    unlink([path fileSystemRepresentation]);
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) return -1;
    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, [path fileSystemRepresentation], sizeof(addr.sun_path) - 1);
    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        close(fd);
        return -1;
    }
    if (listen(fd, 4) != 0) {
        close(fd);
        return -1;
    }
    int flags = fcntl(fd, F_GETFL, 0);
    fcntl(fd, F_SETFL, flags | O_NONBLOCK);
    return fd;
}

// ---------------------------------------------------------------- CMIO warmup
static NSInteger cmio_device_count(void) {
    CMIOObjectPropertyAddress allowAddr = {
        kCMIOHardwarePropertyAllowScreenCaptureDevices,
        kCMIOObjectPropertyScopeGlobal,
        kCMIOObjectPropertyElementMain
    };
    UInt32 allow = 1;
    CMIOObjectSetPropertyData(kCMIOObjectSystemObject, &allowAddr, 0, NULL, sizeof(allow), &allow);

    CMIOObjectPropertyAddress devAddr = {
        kCMIOHardwarePropertyDevices,
        kCMIOObjectPropertyScopeGlobal,
        kCMIOObjectPropertyElementMain
    };
    UInt32 size = 0;
    if (CMIOObjectGetPropertyDataSize(kCMIOObjectSystemObject, &devAddr, 0, NULL, &size) != kCMIOHardwareNoError) {
        return 0;
    }
    return (NSInteger)(size / sizeof(CMIOObjectID));
}

// The iOS device is only visible to AVFoundation once CoreMediaIO has been
// initialised in this process (the DAL plug-in registers it).
static AVCaptureDevice *find_ios_device(void) {
    (void)cmio_device_count();
    NSArray *types = @[
        AVCaptureDeviceTypeBuiltInWideAngleCamera,
        AVCaptureDeviceTypeExternal,
        AVCaptureDeviceTypeContinuityCamera,
        AVCaptureDeviceTypeMicrophone,
    ];
    AVCaptureDeviceDiscoverySession *muxed =
        [AVCaptureDeviceDiscoverySession discoverySessionWithDeviceTypes:types
                                                               mediaType:AVMediaTypeMuxed
                                                                position:AVCaptureDevicePositionUnspecified];
    if (muxed.devices.count > 0) return muxed.devices.firstObject;
    AVCaptureDeviceDiscoverySession *video =
        [AVCaptureDeviceDiscoverySession discoverySessionWithDeviceTypes:types
                                                               mediaType:AVMediaTypeVideo
                                                                position:AVCaptureDevicePositionUnspecified];
    for (AVCaptureDevice *device in video.devices) {
        NSString *name = device.localizedName.lowercaseString;
        if ([name containsString:@"iphone"] || [name containsString:@"ipad"]) return device;
    }
    return nil;
}

static AVAuthorizationStatus camera_auth_status(void) {
    return [AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeVideo];
}

// Wait until the iOS device shows up (the system capture service can take a
// moment to start after the phone is plugged in or after a reboot).
static AVCaptureDevice *wait_for_ios_device(double timeout) {
    NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:timeout];
    AVCaptureDevice *device = nil;
    while (!gStop) {
        device = find_ios_device();
        if (device != nil) return device;
        if ([deadline timeIntervalSinceNow] <= 0) return nil;
        usleep(400 * 1000);
    }
    return nil;
}

// ------------------------------------------------------------------- encoding
static NSData *jpeg_from_pixel_buffer(CVPixelBufferRef pixelBuffer, CGFloat maxWidth, CGFloat quality) {
    CIImage *image = [CIImage imageWithCVPixelBuffer:pixelBuffer];
    if (maxWidth > 0 && image.extent.size.width > maxWidth) {
        CGFloat scale = maxWidth / image.extent.size.width;
        image = [image imageByApplyingTransform:CGAffineTransformMakeScale(scale, scale)];
    }
    static CIContext *context = nil;
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        context = [CIContext contextWithOptions:@{kCIContextUseSoftwareRenderer: @NO}];
    });
    CGColorSpaceRef colorSpace = CGColorSpaceCreateWithName(kCGColorSpaceSRGB);
    CGImageRef cgImage = [context createCGImage:image fromRect:image.extent];
    if (cgImage == NULL) {
        CGColorSpaceRelease(colorSpace);
        return nil;
    }
    NSMutableData *data = [NSMutableData data];
    CGImageDestinationRef destination =
        CGImageDestinationCreateWithData((__bridge CFMutableDataRef)data, CFSTR("public.jpeg"), 1, NULL);
    NSDictionary *options = @{(__bridge NSString *)kCGImageDestinationLossyCompressionQuality: @(quality)};
    CGImageDestinationAddImage(destination, cgImage, (__bridge CFDictionaryRef)options);
    BOOL ok = CGImageDestinationFinalize(destination);
    CFRelease(destination);
    CGImageRelease(cgImage);
    CGColorSpaceRelease(colorSpace);
    return ok ? data : nil;
}

static BOOL write_png(CVPixelBufferRef pixelBuffer, NSString *path) {
    CIImage *image = [CIImage imageWithCVPixelBuffer:pixelBuffer];
    static CIContext *context = nil;
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        context = [CIContext context];
    });
    CGImageRef cgImage = [context createCGImage:image fromRect:image.extent];
    if (cgImage == NULL) return NO;
    NSURL *url = [NSURL fileURLWithPath:path];
    CGImageDestinationRef destination =
        CGImageDestinationCreateWithURL((__bridge CFURLRef)url, CFSTR("public.png"), 1, NULL);
    if (destination == NULL) {
        CGImageRelease(cgImage);
        return NO;
    }
    CGImageDestinationAddImage(destination, cgImage, NULL);
    BOOL ok = CGImageDestinationFinalize(destination);
    CFRelease(destination);
    CGImageRelease(cgImage);
    return ok;
}

// -------------------------------------------------------------------- grabber
@interface IOSEyeGrabber : NSObject <AVCaptureVideoDataOutputSampleBufferDelegate>
@property (nonatomic, strong) AVCaptureSession *session;
@property (nonatomic, strong) NSString *mode;
@property (nonatomic, strong) NSString *outPath;
@property (nonatomic) CGFloat maxWidth;
@property (nonatomic) CGFloat quality;
@property (nonatomic) double minFrameInterval;  // 0 = no throttle
@property (atomic) BOOL done;
@end

@implementation IOSEyeGrabber

- (void)captureOutput:(AVCaptureOutput *)output
    didOutputSampleBuffer:(CMSampleBufferRef)sampleBuffer
           fromConnection:(AVCaptureConnection *)connection {
    if (self.done || gStop) return;
    CVPixelBufferRef pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer);
    if (pixelBuffer == NULL) return;
    gNativeWidth = (int)CVPixelBufferGetWidth(pixelBuffer);
    gNativeHeight = (int)CVPixelBufferGetHeight(pixelBuffer);

    double now = [[NSDate date] timeIntervalSince1970];
    if (self.minFrameInterval > 0 && gLastFrameAt > 0 && now - gLastFrameAt < self.minFrameInterval) {
        return;
    }
    gLastFrameAt = now;
    gFrames += 1;

    if ([self.mode isEqualToString:@"shot"]) {
        BOOL ok = write_png(pixelBuffer, self.outPath);
        if (!ok) set_error(@"failed to write PNG frame");
        self.done = YES;
        [self.session stopRunning];
        return;
    }

    NSData *jpeg = jpeg_from_pixel_buffer(pixelBuffer, self.maxWidth, self.quality);
    if (jpeg == nil) return;
    uint32_t length = htonl((uint32_t)jpeg.length);
    NSMutableData *packet = [NSMutableData dataWithCapacity:jpeg.length + 4];
    [packet appendBytes:&length length:sizeof(length)];
    [packet appendData:jpeg];
    [gClientsLock lock];
    for (IOSEyeClient *client in gClients) {
        [client enqueue:packet];
    }
    [gClientsLock unlock];
}

@end

// --------------------------------------------------------------- synthetic frame
static NSData *synthetic_jpeg(void) {
    const size_t width = 320, height = 640;
    CGColorSpaceRef colorSpace = CGColorSpaceCreateDeviceRGB();
    size_t rowBytes = width * 4;
    uint8_t *buffer = calloc(height, rowBytes);
    if (buffer == NULL) {
        CGColorSpaceRelease(colorSpace);
        return nil;
    }
    CGContextRef context = CGBitmapContextCreate(buffer, width, height, 8, rowBytes, colorSpace,
                                                 kCGImageAlphaPremultipliedLast);
    if (context == NULL) {
        free(buffer);
        CGColorSpaceRelease(colorSpace);
        return nil;
    }
    CGContextSetRGBFillColor(context, 0.15, 0.35, 0.75, 1.0);
    CGContextFillRect(context, CGRectMake(0, 0, width, height));
    CGContextSetRGBFillColor(context, 0.9, 0.9, 0.2, 1.0);
    CGContextFillRect(context, CGRectMake(20, 20, width - 40, 80));
    CGImageRef image = CGBitmapContextCreateImage(context);
    CGContextRelease(context);
    CGColorSpaceRelease(colorSpace);
    free(buffer);
    if (image == NULL) return nil;
    NSMutableData *data = [NSMutableData data];
    CGImageDestinationRef destination =
        CGImageDestinationCreateWithData((__bridge CFMutableDataRef)data, CFSTR("public.jpeg"), 1, NULL);
    NSDictionary *options = @{(__bridge NSString *)kCGImageDestinationLossyCompressionQuality: @0.6};
    CGImageDestinationAddImage(destination, image, (__bridge CFDictionaryRef)options);
    BOOL ok = CGImageDestinationFinalize(destination);
    CFRelease(destination);
    CGImageRelease(image);
    return ok ? data : nil;
}

// ----------------------------------------------------------------------- main
static NSString *env_str(const char *name) {
    const char *value = getenv(name);
    if (value == NULL || value[0] == '\0') return nil;
    return [NSString stringWithUTF8String:value];
}

static double env_double(const char *name, double fallback) {
    NSString *value = env_str(name);
    return value != nil ? value.doubleValue : fallback;
}

// LaunchServices (needed for camera TCC) does not reliably forward argv/env,
// so the Python side writes a small JSON config the app consumes on start.
static NSString *config_path(void) {
    NSString *override = env_str("IOSEYE_CONFIG");
    return override != nil ? override : @"/tmp/phone-ios-eye/config.json";
}

static NSDictionary *load_config(void) {
    NSString *path = config_path();
    NSData *data = [NSData dataWithContentsOfFile:path];
    if (data == nil) return nil;
    NSDictionary *dict = [NSJSONSerialization JSONObjectWithData:data options:0 error:NULL];
    if (![dict isKindOfClass:[NSDictionary class]]) return nil;
    [[NSFileManager defaultManager] removeItemAtPath:path error:NULL];
    return dict;
}

static BOOL parse_args(int argc, char **argv, NSString **mode, NSString **socketPath, NSString **outPath,
                       CGFloat *maxWidth, double *maxFps, CGFloat *quality, double *waitDevice,
                       double *waitPermission) {
    (void)0;
    for (int i = 1; i < argc; i++) {
        NSString *arg = [NSString stringWithUTF8String:argv[i]];
        if ([arg isEqualToString:@"--probe"]) *mode = @"probe";
        else if ([arg isEqualToString:@"--shot"]) *mode = @"shot";
        else if ([arg isEqualToString:@"--stream"]) *mode = @"stream";
        else if ([arg isEqualToString:@"--status-file"] && i + 1 < argc) gStatusPath = [NSString stringWithUTF8String:argv[++i]];
        else if ([arg isEqualToString:@"--socket"] && i + 1 < argc) *socketPath = [NSString stringWithUTF8String:argv[++i]];
        else if ([arg isEqualToString:@"--out"] && i + 1 < argc) *outPath = [NSString stringWithUTF8String:argv[++i]];
        else if ([arg isEqualToString:@"--max-width"] && i + 1 < argc) *maxWidth = atof(argv[++i]);
        else if ([arg isEqualToString:@"--max-fps"] && i + 1 < argc) *maxFps = atof(argv[++i]);
        else if ([arg isEqualToString:@"--quality"] && i + 1 < argc) *quality = atof(argv[++i]);
        else if ([arg isEqualToString:@"--wait-device"] && i + 1 < argc) *waitDevice = atof(argv[++i]);
        else if ([arg isEqualToString:@"--wait-permission"] && i + 1 < argc) *waitPermission = atof(argv[++i]);
    }
    return *mode != nil;
}

int main(int argc, char **argv) {
    @autoreleasepool {
        // The helper is normally launched through LaunchServices (needed for
        // camera TCC), where `open --args` does not reliably forward argv or
        // environment; the Python side therefore writes a JSON config file
        // that is consumed once at startup. argv still works for direct runs.
        NSDictionary *config = load_config();
        NSString *mode = config[@"mode"] ?: env_str("IOSEYE_MODE");
        NSString *socketPath = config[@"socket"] ?: env_str("IOSEYE_SOCKET");
        NSString *outPath = config[@"out"] ?: env_str("IOSEYE_OUT");
        gStatusPath = config[@"status_file"] ?: env_str("IOSEYE_STATUS_FILE");
        CGFloat maxWidth = (CGFloat)(config[@"max_width"] ? [config[@"max_width"] doubleValue]
                                                          : env_double("IOSEYE_MAX_WIDTH", 0));
        CGFloat quality = (CGFloat)(config[@"quality"] ? [config[@"quality"] doubleValue]
                                                       : env_double("IOSEYE_QUALITY", 0.75));
        double maxFps = config[@"max_fps"] ? [config[@"max_fps"] doubleValue]
                                           : env_double("IOSEYE_MAX_FPS", 0);
        double waitDevice = config[@"wait_device"] ? [config[@"wait_device"] doubleValue]
                                                   : env_double("IOSEYE_WAIT_DEVICE", 8.0);
        double waitPermission = config[@"wait_permission"] ? [config[@"wait_permission"] doubleValue]
                                                           : env_double("IOSEYE_WAIT_PERMISSION", 25.0);
        if (!parse_args(argc, argv, &mode, &socketPath, &outPath, &maxWidth, &maxFps, &quality,
                        &waitDevice, &waitPermission)) {
            fprintf(stderr, "usage: IOSEye --probe|--shot|--stream [--status-file PATH] [--socket PATH] "
                            "[--out PATH] [--max-width PX] [--max-fps FPS] [--quality Q]\n");
            return 2;
        }
        gClients = [NSMutableArray array];
        gClientsLock = [NSLock new];
        gStartedAt = [[NSDate date] timeIntervalSince1970];
        signal(SIGTERM, handle_signal);
        signal(SIGINT, handle_signal);
        signal(SIGPIPE, SIG_IGN);

        NSApplication *app = [NSApplication sharedApplication];
        [app setActivationPolicy:NSApplicationActivationPolicyRegular];
        [app finishLaunching];
        [app activateIgnoringOtherApps:YES];

        write_status(mode, NO);

        BOOL synthetic = [config[@"synthetic"] boolValue] || getenv("IOSEYE_SYNTHETIC") != NULL;

        if (synthetic && [mode isEqualToString:@"probe"]) {
            gDeviceName = @"Synthetic";
            gNativeWidth = 320;
            gNativeHeight = 640;
            write_status(mode, YES);
            return 0;
        }
        if (synthetic && [mode isEqualToString:@"stream"]) {
            gDeviceName = @"Synthetic";
            gNativeWidth = 320;
            gNativeHeight = 640;
            NSData *jpeg = synthetic_jpeg();
            if (jpeg == nil || socketPath == nil) {
                set_error(@"synthetic stream setup failed");
                write_status(mode, NO);
                return 9;
            }
            int serverFd = open_unix_server(socketPath);
            if (serverFd < 0) {
                set_error([NSString stringWithFormat:@"cannot create unix socket %@", socketPath]);
                write_status(mode, NO);
                return 9;
            }
            uint32_t length = htonl((uint32_t)jpeg.length);
            NSMutableData *packet = [NSMutableData dataWithCapacity:jpeg.length + 4];
            [packet appendBytes:&length length:sizeof(length)];
            [packet appendData:jpeg];
            double interval = maxFps > 0 ? 1.0 / maxFps : 1.0 / 15.0;
            double last = 0;
            double lastStatus = 0;
            write_status(mode, YES);
            while (!gStop) {
                @autoreleasepool {
                    struct sockaddr_un peer;
                    socklen_t peerLen = sizeof(peer);
                    int clientFd = accept(serverFd, (struct sockaddr *)&peer, &peerLen);
                    if (clientFd >= 0) {
                        int one = 1;
                        setsockopt(clientFd, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof(one));
                        IOSEyeClient *client = [[IOSEyeClient alloc] initWithFd:clientFd];
                        [gClientsLock lock];
                        [gClients addObject:client];
                        [gClientsLock unlock];
                    }
                    double now = [[NSDate date] timeIntervalSince1970];
                    if (now - last >= interval) {
                        last = now;
                        gFrames += 1;
                        gLastFrameAt = now;
                        [gClientsLock lock];
                        for (IOSEyeClient *client in gClients) [client enqueue:packet];
                        [gClientsLock unlock];
                    }
                    if (now - lastStatus > 0.5) {
                        lastStatus = now;
                        write_status(mode, YES);
                    }
                    [[NSRunLoop currentRunLoop] runMode:NSDefaultRunLoopMode
                                             beforeDate:[NSDate dateWithTimeIntervalSinceNow:0.01]];
                }
            }
            close_clients();
            close(serverFd);
            unlink([socketPath fileSystemRepresentation]);
            write_status(mode, YES);
            return 0;
        }

        // Camera permission: first run shows a system prompt; the Python side
        // waits for the status file to become ok.
        AVAuthorizationStatus auth = camera_auth_status();
        if (auth == AVAuthorizationStatusNotDetermined) {
            __block BOOL answered = NO;
            [AVCaptureDevice requestAccessForMediaType:AVMediaTypeVideo
                                     completionHandler:^(BOOL granted) {
                                       (void)granted;
                                       answered = YES;
                                     }];
            NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:waitPermission];
            while (!answered && !gStop && [deadline timeIntervalSinceNow] > 0) {
                [[NSRunLoop currentRunLoop] runMode:NSDefaultRunLoopMode
                                         beforeDate:[NSDate dateWithTimeIntervalSinceNow:0.1]];
            }
            auth = camera_auth_status();
        }
        if (auth != AVAuthorizationStatusAuthorized) {
            set_error([NSString stringWithFormat:
                @"camera permission not granted (status=%ld); allow it in "
                 "System Settings > Privacy & Security > Camera > IOSEye, then retry",
                (long)auth]);
            write_status(mode, NO);
            fprintf(stderr, "%s\n", gError.UTF8String);
            return 3;
        }

        AVCaptureDevice *device = wait_for_ios_device(waitDevice);
        if (device == nil) {
            set_error(@"iPhone screen not found. Make sure the iPhone is connected by a data "
                       "cable, unlocked and trusted; on first use open QuickTime > New Movie "
                       "Recording and pick the iPhone once to start the system capture service.");
            write_status(mode, NO);
            fprintf(stderr, "%s\n", gError.UTF8String);
            return 4;
        }
        gDeviceName = device.localizedName;
        gDeviceModel = device.modelID;
        gDeviceUid = device.uniqueID;

        if ([mode isEqualToString:@"probe"]) {
            write_status(mode, YES);
            return 0;
        }

        NSError *error = nil;
        AVCaptureDeviceInput *input = [AVCaptureDeviceInput deviceInputWithDevice:device error:&error];
        if (input == nil) {
            set_error([NSString stringWithFormat:@"cannot open iPhone capture input: %@", error.localizedDescription]);
            write_status(mode, NO);
            return 5;
        }
        AVCaptureSession *session = [AVCaptureSession new];
        session.sessionPreset = AVCaptureSessionPresetHigh;
        if (![session canAddInput:input]) {
            set_error(@"cannot add iPhone input to capture session");
            write_status(mode, NO);
            return 6;
        }
        [session addInput:input];

        AVCaptureVideoDataOutput *output = [AVCaptureVideoDataOutput new];
        output.videoSettings = @{(NSString *)kCVPixelBufferPixelFormatTypeKey : @(kCVPixelFormatType_32BGRA)};
        IOSEyeGrabber *grabber = [IOSEyeGrabber new];
        grabber.session = session;
        grabber.mode = mode;
        grabber.outPath = outPath;
        grabber.maxWidth = maxWidth;
        grabber.quality = quality;
        grabber.minFrameInterval = maxFps > 0 ? 1.0 / maxFps : 0;
        dispatch_queue_t queue = dispatch_get_global_queue(DISPATCH_QUEUE_PRIORITY_HIGH, 0);
        [output setSampleBufferDelegate:grabber queue:queue];
        if (![session canAddOutput:output]) {
            set_error(@"cannot add video output to capture session");
            write_status(mode, NO);
            return 7;
        }
        [session addOutput:output];

        int serverFd = -1;
        if ([mode isEqualToString:@"stream"]) {
            if (socketPath == nil) {
                set_error(@"--stream needs --socket");
                write_status(mode, NO);
                return 8;
            }
            serverFd = open_unix_server(socketPath);
            if (serverFd < 0) {
                set_error([NSString stringWithFormat:@"cannot create unix socket %@: %s", socketPath, strerror(errno)]);
                write_status(mode, NO);
                return 9;
            }
        }

        [session startRunning];
        write_status(mode, YES);

        double lastStatusWrite = 0;
        while (!gStop) {
            @autoreleasepool {
                if ([mode isEqualToString:@"shot"] && grabber.done) break;
                if (serverFd >= 0) {
                    struct sockaddr_un peer;
                    socklen_t peerLen = sizeof(peer);
                    int clientFd = accept(serverFd, (struct sockaddr *)&peer, &peerLen);
                    if (clientFd >= 0) {
                        int one = 1;
                        setsockopt(clientFd, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof(one));
                        int bufferSize = 4 * 1024 * 1024;
                        setsockopt(clientFd, SOL_SOCKET, SO_SNDBUF, &bufferSize, sizeof(bufferSize));
                        IOSEyeClient *client = [[IOSEyeClient alloc] initWithFd:clientFd];
                        [gClientsLock lock];
                        [gClients addObject:client];
                        [gClientsLock unlock];
                    }
                }
                double now = [[NSDate date] timeIntervalSince1970];
                if (mode && now - lastStatusWrite > 0.5) {
                    lastStatusWrite = now;
                    write_status(mode, gError == nil);
                }
                [[NSRunLoop currentRunLoop] runMode:NSDefaultRunLoopMode
                                         beforeDate:[NSDate dateWithTimeIntervalSinceNow:0.02]];
            }
        }

        [session stopRunning];
        write_status(mode, gError == nil);
        close_clients();
        if (serverFd >= 0) {
            close(serverFd);
            unlink([socketPath fileSystemRepresentation]);
        }
        if ([mode isEqualToString:@"shot"] && grabber.done && gError == nil) {
            return 0;
        }
        return gError == nil ? 0 : 1;
    }
}

/* Ground-truth oracle for the V4L2 ABI values wendycam hardcodes.
 * Compiled and run on linux/arm64 (same ABI as the Jetson targets);
 * run_tests.sh diffs this output against the Mojo implementation's. */
#include <linux/videodev2.h>
#include <stdio.h>
#include <stddef.h>

int main(void) {
    printf("VIDIOC_QUERYCAP=0x%lX\n", (unsigned long)VIDIOC_QUERYCAP);
    printf("VIDIOC_ENUM_FMT=0x%lX\n", (unsigned long)VIDIOC_ENUM_FMT);
    printf("VIDIOC_S_FMT=0x%lX\n", (unsigned long)VIDIOC_S_FMT);
    printf("VIDIOC_REQBUFS=0x%lX\n", (unsigned long)VIDIOC_REQBUFS);
    printf("VIDIOC_QUERYBUF=0x%lX\n", (unsigned long)VIDIOC_QUERYBUF);
    printf("VIDIOC_QBUF=0x%lX\n", (unsigned long)VIDIOC_QBUF);
    printf("VIDIOC_DQBUF=0x%lX\n", (unsigned long)VIDIOC_DQBUF);
    printf("VIDIOC_STREAMON=0x%lX\n", (unsigned long)VIDIOC_STREAMON);
    printf("VIDIOC_STREAMOFF=0x%lX\n", (unsigned long)VIDIOC_STREAMOFF);
    printf("VIDIOC_S_PARM=0x%lX\n", (unsigned long)VIDIOC_S_PARM);
    printf("FMT_MJPEG=0x%X\n", V4L2_PIX_FMT_MJPEG);
    printf("FMT_YUYV=0x%X\n", V4L2_PIX_FMT_YUYV);
    printf("CAP_VIDEO_CAPTURE=0x%X\n", V4L2_CAP_VIDEO_CAPTURE);
    printf("CAP_STREAMING=0x%X\n", V4L2_CAP_STREAMING);
    printf("BUF_TYPE_VIDEO_CAPTURE=%d\n", V4L2_BUF_TYPE_VIDEO_CAPTURE);
    printf("MEMORY_MMAP=%d\n", V4L2_MEMORY_MMAP);
    printf("FIELD_NONE=%d\n", V4L2_FIELD_NONE);
    printf("sizeof_capability=%zu\n", sizeof(struct v4l2_capability));
    printf("off_capability_card=%zu\n", offsetof(struct v4l2_capability, card));
    printf("off_capability_device_caps=%zu\n", offsetof(struct v4l2_capability, device_caps));
    printf("sizeof_format=%zu\n", sizeof(struct v4l2_format));
    printf("off_format_pix=%zu\n", offsetof(struct v4l2_format, fmt.pix));
    printf("off_pix_pixelformat=%zu\n",
           offsetof(struct v4l2_format, fmt.pix.pixelformat));
    printf("off_pix_sizeimage=%zu\n", offsetof(struct v4l2_format, fmt.pix.sizeimage));
    printf("sizeof_requestbuffers=%zu\n", sizeof(struct v4l2_requestbuffers));
    printf("sizeof_buffer=%zu\n", sizeof(struct v4l2_buffer));
    printf("off_buffer_bytesused=%zu\n", offsetof(struct v4l2_buffer, bytesused));
    printf("off_buffer_memory=%zu\n", offsetof(struct v4l2_buffer, memory));
    printf("off_buffer_m_offset=%zu\n", offsetof(struct v4l2_buffer, m.offset));
    printf("off_buffer_length=%zu\n", offsetof(struct v4l2_buffer, length));
    printf("sizeof_streamparm=%zu\n", sizeof(struct v4l2_streamparm));
    printf("off_parm_timeperframe=%zu\n",
           offsetof(struct v4l2_streamparm, parm.capture.timeperframe));
    return 0;
}

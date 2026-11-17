// No toolkit headers are needed: NVRTC provides the CUDA language builtins.
__device__ float from_half(unsigned short bits) {
    float value;
    asm("cvt.f32.f16 %0, %1;" : "=f"(value) : "h"(bits));
    return value;
}

__device__ unsigned short to_half(float value) {
    unsigned short bits;
    asm("cvt.rn.f16.f32 %0, %1;" : "=h"(bits) : "f"(value));
    return bits;
}

__device__ float half_at(const unsigned char* block, int offset) {
    return from_half(block[offset] | (block[offset + 1] << 8));
}

__device__ float weight_at(const unsigned char* row, int col) {
#if KIND == 6  // Q6_K
    const unsigned char* b = row + (col / 256) * 210;
    int i = col % 256, half = i / 128, group = (i % 128) / 32, lane = i % 32;
    int low = b[half * 64 + (group % 2) * 32 + lane];
    int high = b[128 + half * 32 + lane];
    int q = ((low >> ((group / 2) * 4)) & 15) | (((high >> (group * 2)) & 3) << 4);
    return (half_at(b, 208) * (float)(signed char)b[192 + i / 16]) * (float)(q - 32);
#elif KIND == 3  // Q8_0
    const unsigned char* b = row + (col / 32) * 34;
    return half_at(b, 0) * (float)(signed char)b[2 + col % 32];
#elif KIND < 3  // Q4_0, Q4_1, Q5_0
    const int size = KIND == 0 ? 18 : (KIND == 1 ? 20 : 22);
    const int start = KIND == 0 ? 2 : (KIND == 1 ? 4 : 6);
    const unsigned char* b = row + (col / 32) * size;
    int i = col % 32;
    int q = (b[start + i % 16] >> ((i / 16) * 4)) & 15;
    if (KIND == 2) q |= ((b[2 + i / 8] >> (i % 8)) & 1) << 4;
    float value = half_at(b, 0) * (float)(q - (KIND == 0 ? 8 : (KIND == 2 ? 16 : 0)));
    return KIND == 1 ? value + half_at(b, 2) : value;
#else  // Q4_K, Q5_K
    const unsigned char* b = row + (col / 256) * (KIND == 4 ? 144 : 176);
    int i = col % 256, group = i / 32, lane = i % 32;
    int a = b[4 + group % 4], c = b[12 + group % 4], z = b[8 + group % 4];
    int scale = group < 4 ? a & 63 : (c & 15) | ((a >> 2) & 48);
    int minimum = group < 4 ? z & 63 : (c >> 4) | ((z >> 2) & 48);
    int q = (b[(KIND == 4 ? 16 : 48) + (group / 2) * 32 + lane] >> ((group % 2) * 4)) & 15;
    if (KIND == 5) q |= ((b[16 + lane] >> group) & 1) << 4;
    return (half_at(b, 0) * (float)scale) * (float)q - half_at(b, 2) * (float)minimum;
#endif
}

extern "C" __global__ void dequantize(const unsigned char* w, unsigned short* y,
                                     int rows, int width, int stride) {
    int col = blockIdx.x * blockDim.x + threadIdx.x;
    int row = blockIdx.y + blockIdx.z * 65535;
    if (row < rows && col < width)
        y[(long long)row * width + col] = to_half(weight_at(w + (long long)row * stride, col));
}

extern "C" __global__ void gemv(const unsigned short* x, const unsigned char* w,
                               unsigned short* y, int rows, int width, int stride) {
    int row = blockIdx.x * 4 + threadIdx.x / 32;
    int lane = threadIdx.x % 32;
    float sum = 0;
    if (row < rows) {
        const unsigned char* weights = w + (long long)row * stride;
        const unsigned short* input = x + (long long)blockIdx.y * width;
        for (int col = lane; col < width; col += 32) {
            // Match the reference's FP16 weight rounding and FP32 accumulation.
            float weight = from_half(to_half(weight_at(weights, col)));
            sum += weight * from_half(input[col]);
        }
    }
    for (int offset = 16; offset > 0; offset /= 2)
        sum += __shfl_down_sync(0xffffffff, sum, offset);
    if (row < rows && lane == 0)
        y[(long long)blockIdx.y * rows + row] = to_half(sum);
}

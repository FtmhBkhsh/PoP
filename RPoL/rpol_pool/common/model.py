"""
Model definitions: a small CIFAR-style ResNet with an AMLayer prepended, and
a helper to shuffle+partition the dataset into i.i.d worker sub-datasets
(paper Sec. III-A / VII-A).
"""
import torch
import torch.nn as nn
import torchvision

from .am_layer import AMLayer


class BasicBlock(nn.Module):
    expansion = 1

    def __init__(self, in_planes, planes, stride=1):
        super().__init__()
        self.conv1 = nn.Conv2d(in_planes, planes, 3, stride, 1, bias=False)
        self.bn1 = nn.BatchNorm2d(planes)
        self.conv2 = nn.Conv2d(planes, planes, 3, 1, 1, bias=False)
        self.bn2 = nn.BatchNorm2d(planes)
        self.shortcut = nn.Sequential()
        if stride != 1 or in_planes != planes * self.expansion:
            self.shortcut = nn.Sequential(
                nn.Conv2d(in_planes, planes * self.expansion, 1, stride, bias=False),
                nn.BatchNorm2d(planes * self.expansion),
            )

    def forward(self, x):
        out = torch.relu(self.bn1(self.conv1(x)))
        out = self.bn2(self.conv2(out))
        out += self.shortcut(x)
        return torch.relu(out)


class ResNetCIFAR(nn.Module):
    """Small ResNet (ResNet18-style) for 32x32 inputs, e.g. CIFAR-10/100."""

    def __init__(self, num_blocks=(2, 2, 2, 2), num_classes=10, am_address: str = None,
                 am_c: float = 0.5):
        super().__init__()
        self.am_layer = AMLayer(am_address, in_channels=3, out_channels=64,
                                 kernel_size=3, c=am_c) if am_address else None
        in_ch = 64 if am_address else 3
        self.in_planes = 64
        self.stem = nn.Sequential(
            nn.Conv2d(in_ch, 64, 3, 1, 1, bias=False),
            nn.BatchNorm2d(64),
            nn.ReLU(inplace=True),
        )
        self.layer1 = self._make_layer(64, num_blocks[0], 1)
        self.layer2 = self._make_layer(128, num_blocks[1], 2)
        self.layer3 = self._make_layer(256, num_blocks[2], 2)
        self.layer4 = self._make_layer(512, num_blocks[3], 2)
        self.pool = nn.AdaptiveAvgPool2d(1)
        self.fc = nn.Linear(512, num_classes)

    def _make_layer(self, planes, num_blocks, stride):
        strides = [stride] + [1] * (num_blocks - 1)
        layers = []
        for s in strides:
            layers.append(BasicBlock(self.in_planes, planes, s))
            self.in_planes = planes
        return nn.Sequential(*layers)

    def forward(self, x):
        if self.am_layer is not None:
            x = self.am_layer(x)
        out = self.stem(x)
        out = self.layer1(out)
        out = self.layer2(out)
        out = self.layer3(out)
        out = self.layer4(out)
        out = self.pool(out).flatten(1)
        return self.fc(out)


def build_model(arch: str, num_classes: int, am_address: str = None, am_c: float = 0.5):
    if arch == "resnet18":
        return ResNetCIFAR((2, 2, 2, 2), num_classes, am_address, am_c)
    if arch == "resnet50":
        # deeper stack of BasicBlocks as a stand-in for the paper's ResNet50
        return ResNetCIFAR((3, 4, 6, 3), num_classes, am_address, am_c)
    raise ValueError(f"unknown arch {arch}")


class SyntheticDataset(torch.utils.data.Dataset):
    """Deterministic random 32x32x3 images + labels — no download needed.
    Useful for smoke-testing the manager/worker protocol before pointing it
    at real CIFAR data on your 5 nodes (set training.dataset = "synthetic")."""

    def __init__(self, size: int = 2000, num_classes: int = 10, seed: int = 0):
        g = torch.Generator().manual_seed(seed)
        self.x = torch.rand(size, 3, 32, 32, generator=g)
        self.y = torch.randint(0, num_classes, (size,), generator=g)

    def __len__(self):
        return self.x.shape[0]

    def __getitem__(self, idx):
        return self.x[idx], int(self.y[idx])


def load_dataset(name: str, root: str = "./data", train: bool = True, num_classes: int = 10):
    if name == "synthetic":
        return SyntheticDataset(size=2000, num_classes=num_classes)
    tfm = torchvision.transforms.Compose([
        torchvision.transforms.ToTensor(),
    ])
    if name == "cifar10":
        return torchvision.datasets.CIFAR10(root=root, train=train, download=True, transform=tfm)
    if name == "cifar100":
        return torchvision.datasets.CIFAR100(root=root, train=train, download=True, transform=tfm)
    raise ValueError(f"unknown dataset {name}")


def partition_indices(dataset_size: int, num_workers: int, seed: int = 1234) -> list:
    """Randomly shuffle then split into `num_workers` equal i.i.d subsets
    (paper Sec. III-A, step 1 in Fig. 2)."""
    g = torch.Generator().manual_seed(seed)
    perm = torch.randperm(dataset_size, generator=g).tolist()
    chunk = dataset_size // num_workers
    parts = []
    for i in range(num_workers):
        start = i * chunk
        end = (i + 1) * chunk if i < num_workers - 1 else dataset_size
        parts.append(perm[start:end])
    return parts

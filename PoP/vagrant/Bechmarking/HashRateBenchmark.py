import hashlib, time
import matplotlib.pyplot as plt

def hash_rate_test(seconds=60):
    lst=[]
    for _ in range(10):
        count = 0
        start = time.time()
        while time.time() - start < seconds:
            hashlib.sha256(b"test").hexdigest()
            count += 1
        # print(f"Approx. {count / seconds:.0f} hashes per second")
        lst.append(count / seconds)
    average = sum(lst) / len(lst)
    print(f"Approx. {average:.0f} hashes per second")  
    plt.plot(lst)
    plt.title('Hash rate')
    plt.ylabel('time')
    plt.show()  

hash_rate_test()
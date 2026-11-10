"""Elige IDs de DICT_4X4_50 con máxima distancia de Hamming mutua (considerando 4 rotaciones)."""
import cv2, numpy as np, itertools
D = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
bits = [cv2.aruco.Dictionary.getBitsFromByteList(D.bytesList[i:i+1], 4) for i in range(50)]
def rots(b): return [np.rot90(b, k) for k in range(4)]
def dist(a, b): return min(int((a != r).sum()) for r in rots(b))
M = np.array([[dist(bits[i], bits[j]) if i != j else 99 for j in range(50)] for i in range(50)])
self_rot = [min(int((bits[i] != np.rot90(bits[i], k)).sum()) for k in (1, 2, 3)) for i in range(50)]
print("dist mínima global del diccionario:", M.min())
print("IDs actuales {1,2,3}: min dist mutua", min(M[1,2], M[1,3], M[2,3]), "auto-rot", [self_rot[i] for i in (1,2,3)])
best = max(itertools.combinations(range(50), 3), key=lambda c: (min(M[a,b] for a,b in itertools.combinations(c,2)), min(self_rot[i] for i in c), -sum(c)))
print("mejor terna:", best, "min dist mutua", min(M[a,b] for a,b in itertools.combinations(best,2)), "auto-rot", [self_rot[i] for i in best])
best4 = max(itertools.combinations(range(50), 4), key=lambda c: (min(M[a,b] for a,b in itertools.combinations(c,2)), min(self_rot[i] for i in c), -sum(c)))
print("mejor cuaterna (3 autos + 1 de reserva):", best4, "min dist", min(M[a,b] for a,b in itertools.combinations(best4,2)))
# distancia de cada ID a todos los demás (riesgo de confusión con un ID no usado)
